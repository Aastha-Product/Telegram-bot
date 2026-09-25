"""Entrypoint: the Telegram application in three hosting modes.

- Local: `python app.py` long-polls (no public URL needed).
- Always-on host (Railway/Render): `python app.py` with PUBLIC_URL set runs a webhook server.
- Vercel: imports this module and serves the ASGI `app` below; each request does its work
  before replying, because a serverless function may be frozen once the response is sent.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from typing import Any

import tornado.httpserver
import tornado.web

from telegram import Bot, Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
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
# Anything spoken: voice notes, audio files, round video messages, audio attachments.
RECORDING = filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.Document.AUDIO


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
                                     settings.webhook_secret or "", settings.database_url or "",
                                     settings.cron_secret or ""]))
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


def build_application(serverless: bool = False) -> Application:
    """The bot with all handlers. Serverless builds skip the scheduler and startup hook (Vercel Cron retries)."""
    builder = Application.builder().token(config.settings.telegram_bot_token)
    builder = builder.job_queue(None) if serverless else builder.post_init(on_startup)
    application = builder.build()
    settings = config.settings
    # Group -1 runs before everything else: record the shape of every update, never its content.
    application.add_handler(TypeHandler(Update, log_update), group=-1)
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
        MessageHandler(filters.UpdateType.MESSAGE & RECORDING & meera_in_review_chat, on_private_voice)
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND & meera_in_review_chat,
            review.handle_review_message,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & ~filters.TEXT & ~RECORDING & ~filters.COMMAND & meera_in_review_chat,
            ingest.handle_private_other,
        )
    )
    application.add_error_handler(on_error)
    if not serverless:
        schedule_sweep(application)
    return application


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture the note, then process it immediately in the background (triage, scorecard, draft)."""
    note_id = await ingest.handle_channel_post(update, context)
    if note_id is not None:
        await _process_now_or_later(update, context, note_id)


async def _process_now_or_later(update: Update, context: ContextTypes.DEFAULT_TYPE, note_id: int) -> None:
    """Always-on hosts process in the background; serverless must finish before replying to Telegram."""
    if config.settings.serverless:
        await pipeline.process_note(context.bot, note_id)
    else:
        context.application.create_task(pipeline.process_note(context.bot, note_id), update=update)


def describe_update(update: Update) -> str:
    """Shape of an update for diagnostics: kind, chat, sender and media type. No message content."""
    message = update.effective_message
    if update.callback_query is not None:
        return f"kind=button from={update.callback_query.from_user.id}"
    if message is None:
        return "kind=other"
    kind = "channel_post" if update.channel_post is not None else "message"
    media = next((m for m in ("voice", "audio", "video_note", "document", "text", "photo", "sticker", "video")
                  if getattr(message, m, None)), "other")
    sender = message.from_user.id if message.from_user else None
    return f"kind={kind} chat={message.chat.id} chat_type={message.chat.type} from={sender} media={media}"


async def log_update(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(update, Update):
        log.info("app.update_received update_id=%s %s", update.update_id, describe_update(update))


async def on_private_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Meera can also send voice notes straight to the bot chat; same flow as the channel."""
    note_id = await ingest.handle_private_voice(update, context)
    if note_id is not None:
        await _process_now_or_later(update, context, note_id)


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
            url=webhook_url(), secret_token=settings.webhook_secret, allowed_updates=ALLOWED_UPDATES,
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


def webhook_url() -> str:
    return f"{config.settings.public_url}/telegram/{config.settings.webhook_secret}"


# --- Vercel (serverless) entrypoint -------------------------------------------------------------

_serverless_ready = False


def _prepare_serverless() -> None:
    """Once per cold start: logging, assets, and the database (migrations are idempotent)."""
    global _serverless_ready
    if not _serverless_ready:
        setup_logging()
        check_assets()
        db.init_db()
        _serverless_ready = True


async def _process_serverless_update(body: bytes) -> None:
    application = build_application(serverless=True)
    async with application:  # initialize/shutdown per request: nothing may outlive it
        await application.process_update(Update.de_json(json.loads(body), application.bot))


async def serve_webhook(secret_header: str | None, body: bytes) -> tuple[int, str]:
    status, update = parse_webhook(secret_header, body, None)  # authenticate before any Telegram call
    if update is None:
        log.warning("app.webhook_rejected status=%s", status)
        return status, "rejected"
    _prepare_serverless()
    await _process_serverless_update(body)
    # 200 even if a handler failed: failures are logged and retried by the cron, and a non-2xx
    # would make Telegram redeliver the same update over and over.
    return 200, "ok"


async def serve_cron(authorization: str | None) -> tuple[int, str]:
    expected = f"Bearer {config.settings.cron_secret}" if config.settings.cron_secret else ""
    if not expected or not hmac.compare_digest((authorization or "").encode(), expected.encode()):
        return 401, "unauthorised"
    _prepare_serverless()
    async with Bot(config.settings.telegram_bot_token) as bot:
        outcome = await pipeline.run_sweep(bot, pipeline.sweep_slot(datetime.now(UTC)))
    return 200, outcome


async def route(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
    if method == "GET" and path == "/healthz":
        return 200, "ok"
    if method == "POST" and config.settings.webhook_secret and path == f"/telegram/{config.settings.webhook_secret}":
        return await serve_webhook(headers.get(WEBHOOK_SECRET_HEADER.lower()), body)
    if method == "GET" and path == "/cron/sweep":
        return await serve_cron(headers.get("authorization"))
    return 404, "not found"


async def _read_body(receive: Any) -> bytes | None:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            return None
        if not message.get("more_body"):
            return body


async def asgi_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """ASGI entrypoint for Vercel."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    body = await _read_body(receive)
    if body is None:
        status, text = 413, "too large"
    else:
        try:
            status, text = await route(scope["method"], scope["path"], headers, body)
        except Exception:
            log.exception("app.request_failed path_kind=%s", "telegram" if scope["path"].startswith("/telegram/") else scope["path"])
            status, text = 500, "error"
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": text.encode()})


# Vercel looks for a top-level variable named `app` in app.py.
app = asgi_app


# --- command line ----------------------------------------------------------------------------------


async def register_webhook() -> None:
    """One-time step after deploying to Vercel: point Telegram at the deployment's webhook URL."""
    settings = config.settings
    if not settings.public_url or not settings.webhook_secret:
        raise SystemExit("Set PUBLIC_URL and WEBHOOK_SECRET in .env first.")
    async with Bot(settings.telegram_bot_token) as bot:
        await bot.set_webhook(url=webhook_url(), secret_token=settings.webhook_secret,
                              allowed_updates=ALLOWED_UPDATES)
        info = await bot.get_webhook_info()
    print(f"Webhook set to {settings.public_url}/telegram/*** (pending updates: {info.pending_update_count})")


def main() -> None:
    """Start the bot: webhook when PUBLIC_URL is set (production), long-polling otherwise (local)."""
    if sys.argv[1:] == ["set-webhook"]:
        asyncio.run(register_webhook())
        return
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
