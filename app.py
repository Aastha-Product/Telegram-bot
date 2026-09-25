"""Entrypoint: builds the Telegram application (polling locally, webhook in prod)."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import signal

import tornado.httpserver
import tornado.web

from telegram import Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db
import draft
import gemini_client
import ingest
import pipeline
import review

log = logging.getLogger(__name__)

WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_WEBHOOK_BODY_BYTES = 1_000_000

REQUIRED_PROMPTS: tuple[str, ...] = ("triage", "draft", "revise", "repair", "transcribe", "voice_skill", "qa",
                                     "suggest")

ALLOWED_UPDATES: list[str] = [Update.CHANNEL_POST, Update.MESSAGE, Update.CALLBACK_QUERY]
FIRST_SWEEP_SECONDS = 60


class RedactSecrets(logging.Filter):
    """Scrub secrets from every log line, whichever library emitted it."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "***")
        record.msg, record.args = message, None
        if record.exc_info:
            # Render the traceback now so it goes through redaction too.
            text = logging.Formatter().formatException(record.exc_info)
            for secret in self._secrets:
                text = text.replace(secret, "***")
            record.exc_text, record.exc_info = text, None
        return True


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    settings = config.settings
    # The webhook secret is part of the URL path, which tornado's access log would otherwise print.
    handler.addFilter(RedactSecrets([settings.telegram_bot_token, settings.gemini_api_key,
                                     settings.webhook_secret or ""]))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("tornado.access").setLevel(logging.WARNING)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last-resort handler: log and keep running."""
    update_id = update.update_id if isinstance(update, Update) else None
    log.error("app.unhandled_error update_id=%s", update_id, exc_info=context.error)


def check_assets() -> None:
    """Fail fast if the voice corpus or any prompt is missing: drafting without them is a defect."""
    pieces = draft.load_corpus()
    for name in REQUIRED_PROMPTS:
        if not gemini_client.load_prompt(name).strip():
            raise ValueError(f"prompt {name!r} is empty")
    log.info("app.self_check assets=ok corpus_pieces=%d prompts=%d", len(pieces), len(REQUIRED_PROMPTS))


async def check_review_chat(application: Application) -> bool:
    """Warn loudly if drafts can't reach Meera (usually: she hasn't pressed Start yet)."""
    try:
        await application.bot.send_chat_action(config.settings.telegram_review_chat_id, "typing")
    except TelegramError as exc:
        log.warning("app.self_check review_chat=unreachable error=%s hint='open the bot and press Start'",
                    type(exc).__name__)
        return False
    log.info("app.self_check review_chat=ok")
    return True


async def on_startup(application: Application) -> None:
    """getMe already ran in initialize(); check delivery, then pick up work left from before a restart."""
    log.info("app.self_check bot=@%s", application.bot.username)
    if await check_review_chat(application):
        await review.deliver_waiting(application.bot)
    await ingest.transcribe_pending(application.bot)


def build_application() -> Application:
    application = (
        Application.builder()
        .token(config.settings.telegram_bot_token)
        .post_init(on_startup)
        .build()
    )
    settings = config.settings
    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST & filters.Chat(chat_id=settings.telegram_chat_id),
            on_channel_post,
        )
    )
    # Filters narrow what reaches the handlers; each handler re-checks Meera's id in code.
    meera_in_review_chat = filters.Chat(chat_id=settings.telegram_review_chat_id) & filters.User(
        user_id=settings.meera_user_id)
    application.add_handler(CommandHandler("start", review.handle_start, filters=meera_in_review_chat))
    application.add_handler(CommandHandler("run", pipeline.handle_run_command, filters=meera_in_review_chat))
    application.add_handler(CallbackQueryHandler(review.handle_callback, pattern=review.CALLBACK_RE))
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND & meera_in_review_chat,
            review.handle_review_message,
        )
    )
    application.add_error_handler(on_error)
    schedule_sweep(application)
    return application


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture the note, then process it immediately in the background (triage, scorecard, draft)."""
    note_id = await ingest.handle_channel_post(update, context)
    if note_id is not None:
        context.application.create_task(pipeline.process_note(context.bot, note_id), update=update)


def schedule_sweep(application: Application) -> None:
    """A periodic retry of anything that failed (Gemini or Telegram down, transcription pending)."""
    if application.job_queue is None:
        raise RuntimeError("JobQueue unavailable: install python-telegram-bot[job-queue]")
    application.job_queue.run_repeating(
        pipeline.sweep_job,
        interval=config.settings.sweep_interval_minutes * 60,
        first=FIRST_SWEEP_SECONDS,
        name="retry-sweep",
    )


def parse_webhook(secret_header: str | None, body: bytes, bot: object) -> tuple[int, Update | None]:
    """HTTP status plus the update for one webhook request. Only Telegram knows the secret token."""
    expected = config.settings.webhook_secret or ""
    supplied = secret_header or ""
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        return 403, None
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("update must be a JSON object")
        return 200, Update.de_json(data, bot)
    except Exception:  # any malformed payload is a 400, never a crashed handler
        return 400, None


def make_web_app(application: Application) -> tornado.web.Application:
    """Two routes only: the secret webhook path and a health check."""

    class TelegramWebhook(tornado.web.RequestHandler):
        async def post(self) -> None:
            status, update = parse_webhook(self.request.headers.get(WEBHOOK_SECRET_HEADER),
                                           self.request.body, application.bot)
            if update is None:
                log.warning("app.webhook_rejected status=%s", status)
            else:
                await application.update_queue.put(update)
            self.set_status(status)

    class Health(tornado.web.RequestHandler):
        def get(self) -> None:
            self.write("ok")

    return tornado.web.Application([
        (f"/telegram/{config.settings.webhook_secret}", TelegramWebhook),
        (r"/healthz", Health),
    ])


async def run_webhook(application: Application) -> None:
    """Production mode: register the webhook with Telegram and serve it until SIGTERM/SIGINT."""
    settings = config.settings
    server = tornado.httpserver.HTTPServer(make_web_app(application), max_body_size=MAX_WEBHOOK_BODY_BYTES)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows has no loop signal handlers; Ctrl+C still raises KeyboardInterrupt there.
    async with application:
        await application.bot.set_webhook(
            url=f"{settings.public_url}/telegram/{settings.webhook_secret}",
            secret_token=settings.webhook_secret,
            allowed_updates=ALLOWED_UPDATES,
        )
        await application.start()
        server.listen(settings.port, address="0.0.0.0")
        log.info("app.webhook_listening port=%s", settings.port)
        # post_init only runs under PTB's own runners, so the startup checks are called here.
        await on_startup(application)
        try:
            await stop.wait()
        finally:
            server.stop()
            await application.stop()


def main() -> None:
    """Start the bot: webhook when PUBLIC_URL is set (production), long-polling otherwise (local)."""
    setup_logging()
    try:
        check_assets()
    except Exception:
        log.critical("app.startup_failed reason=missing_assets", exc_info=True)
        raise SystemExit(1) from None
    db.init_db()
    mode = "webhook" if config.settings.webhook_mode else "polling"
    log.info("app.starting mode=%s capture_chat=%s db=%s", mode,
             config.settings.telegram_chat_id, config.settings.db_path)
    try:
        if config.settings.webhook_mode:
            asyncio.run(run_webhook(build_application()))
        else:
            # Pending updates are Meera's notes, so they are never dropped on startup.
            build_application().run_polling(allowed_updates=ALLOWED_UPDATES, bootstrap_retries=3)
    except InvalidToken:
        # PTB puts the token in this exception's text, so it is never printed.
        log.critical("app.startup_failed reason=invalid_bot_token hint='check TELEGRAM_BOT_TOKEN'")
        raise SystemExit(1) from None
    except Exception:
        # Route through logging so the traceback is redacted instead of printed raw.
        log.critical("app.crashed", exc_info=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
