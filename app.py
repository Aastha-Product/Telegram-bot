"""Entrypoint: builds the Telegram application (polling locally, webhook in prod)."""

from __future__ import annotations

import logging

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

REQUIRED_PROMPTS: tuple[str, ...] = ("triage", "draft", "revise", "repair", "transcribe", "voice_skill")

ALLOWED_UPDATES: list[str] = [Update.CHANNEL_POST, Update.MESSAGE, Update.CALLBACK_QUERY]
# PTB's run_daily numbers days from Sunday = 0.
PTB_DAYS: dict[str, int] = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


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
    handler.addFilter(RedactSecrets([config.settings.telegram_bot_token, config.settings.gemini_api_key]))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


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
            ingest.handle_channel_post,
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
    schedule_drafts(application)
    return application


def schedule_drafts(application: Application) -> None:
    """Register the Mon/Wed/Fri (by default) draft job in the configured timezone."""
    settings = config.settings
    if application.job_queue is None:
        raise RuntimeError("JobQueue unavailable: install python-telegram-bot[job-queue]")
    application.job_queue.run_daily(
        pipeline.scheduled_job,
        time=settings.schedule_time.replace(tzinfo=settings.timezone),
        days=tuple(PTB_DAYS[d] for d in settings.schedule_days),
        name="draft-pipeline",
    )


def main() -> None:
    """Start the bot in long-polling mode (local dev)."""
    setup_logging()
    try:
        check_assets()
    except Exception:
        log.critical("app.startup_failed reason=missing_assets", exc_info=True)
        raise SystemExit(1) from None
    db.init_db()
    log.info("app.starting mode=polling capture_chat=%s db=%s",
             config.settings.telegram_chat_id, config.settings.db_path)
    try:
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
