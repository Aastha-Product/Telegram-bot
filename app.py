"""Entrypoint: builds the Telegram application (polling locally, webhook in prod)."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.error import InvalidToken
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import config
import db
import ingest

log = logging.getLogger(__name__)

ALLOWED_UPDATES: list[str] = [Update.CHANNEL_POST]


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


def build_application() -> Application:
    application = Application.builder().token(config.settings.telegram_bot_token).build()
    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST & filters.Chat(chat_id=config.settings.telegram_chat_id),
            ingest.handle_channel_post,
        )
    )
    application.add_error_handler(on_error)
    return application


def main() -> None:
    """Start the bot in long-polling mode (local dev)."""
    setup_logging()
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
