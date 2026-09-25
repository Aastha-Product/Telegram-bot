"""Typed settings loaded from the environment (and `.env` in local dev).

Import fails fast with a ConfigError naming every missing or invalid variable,
so the service never runs half-configured.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

REQUIRED_VARS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_REVIEW_CHAT_ID",
    "MEERA_USER_ID",
    "GEMINI_API_KEY",
)

DEFAULTS: dict[str, str] = {
    "TRIAGE_MODEL": "gemini-3.5-flash-lite",
    "DRAFT_MODEL": "gemini-3.5-flash",
    "TRANSCRIBE_MODEL": "gemini-3.5-flash",
    "DB_PATH": "skinstinct.db",
    "TIMEZONE": "Asia/Kolkata",
    "TRIAGE_THRESHOLD": "8",
    "TRIAGE_MIN_WORDS": "12",
    "NEWS_MAX_AGE_DAYS": "14",
    "NEWS_MAX_ITEMS": "2",
    "DRAFT_MIN_CHARS": "200",
    "DRAFT_MAX_CHARS": "3000",
    "PORT": "8080",
    "TRANSCRIPT_MAX_UNCLEAR_RATIO": "0.2",
    "QA_MODEL": "gemini-3.5-flash",
    "SWEEP_INTERVAL_MINUTES": "60",
    "MAX_REGENERATIONS": "3",
}

# Telegram's secret_token allows 1-256 of these characters; we also require some length.
WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")

class ConfigError(Exception):
    """Raised when required settings are missing or invalid."""


@dataclass(frozen=True, repr=False)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: int
    telegram_review_chat_id: int
    meera_user_id: int
    gemini_api_key: str
    triage_model: str
    draft_model: str
    transcribe_model: str
    db_path: Path
    timezone: ZoneInfo
    triage_threshold: float
    triage_min_words: int
    news_max_age_days: int
    news_max_items: int
    draft_min_chars: int
    draft_max_chars: int
    public_url: str | None = None
    webhook_secret: str | None = None
    port: int = 8080
    transcript_max_unclear_ratio: float = 0.2
    qa_model: str = "gemini-3.5-flash"
    sweep_interval_minutes: int = 60
    max_regenerations: int = 3

    @property
    def webhook_mode(self) -> bool:
        return self.public_url is not None

    def __repr__(self) -> str:
        return (
            "Settings("
            f"telegram_bot_token={_mask(self.telegram_bot_token)}, "
            f"telegram_chat_id={self.telegram_chat_id}, "
            f"telegram_review_chat_id={self.telegram_review_chat_id}, "
            f"meera_user_id={self.meera_user_id}, "
            f"gemini_api_key={_mask(self.gemini_api_key)}, "
            f"triage_model={self.triage_model!r}, "
            f"draft_model={self.draft_model!r}, "
            f"transcribe_model={self.transcribe_model!r}, "
            f"db_path={str(self.db_path)!r}, "
            f"timezone={self.timezone.key!r}, "
            f"triage_threshold={self.triage_threshold}, "
            f"triage_min_words={self.triage_min_words}, "
            f"news=[{self.news_max_age_days}d, {self.news_max_items} items], "
            f"draft_chars=[{self.draft_min_chars}, {self.draft_max_chars}], "
            f"public_url={self.public_url!r}, "
            f"webhook_secret={_mask(self.webhook_secret or '')}, "
            f"port={self.port}, "
            f"qa_model={self.qa_model!r}, "
            f"transcript_max_unclear_ratio={self.transcript_max_unclear_ratio}, "
            f"sweep_interval_minutes={self.sweep_interval_minutes}, "
            f"max_regenerations={self.max_regenerations})"
        )

    __str__ = __repr__


def _mask(secret: str) -> str:
    return "'***'" if secret else "''"


def _parse_int(name: str, raw: str, errors: list[str]) -> int:
    try:
        return int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer")
        return 0


def _parse_float(name: str, raw: str, errors: list[str]) -> float:
    try:
        return float(raw)
    except ValueError:
        errors.append(f"{name} must be a number")
        return 0.0


def _parse_zone(raw: str, errors: list[str]) -> ZoneInfo:
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"TIMEZONE {raw!r} is not a known IANA zone")
        return ZoneInfo("UTC")


def load_settings(env: Mapping[str, str]) -> Settings:
    """Build Settings from a mapping; raise ConfigError listing every problem."""
    missing = [name for name in REQUIRED_VARS if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )

    def get(name: str) -> str:
        return env.get(name, "").strip() or DEFAULTS[name]

    errors: list[str] = []

    capture_raw = env["TELEGRAM_CHAT_ID"].strip()
    # Channel ids are always -100…; a positive id means the wrong chat was copied.
    if not capture_raw.startswith("-100"):
        errors.append("TELEGRAM_CHAT_ID must be the capture channel id, starting with -100")
    capture_id = _parse_int("TELEGRAM_CHAT_ID", capture_raw, errors)
    review_id = _parse_int("TELEGRAM_REVIEW_CHAT_ID", env["TELEGRAM_REVIEW_CHAT_ID"].strip(), errors)
    meera_id = _parse_int("MEERA_USER_ID", env["MEERA_USER_ID"].strip(), errors)
    if meera_id < 0:
        errors.append("MEERA_USER_ID must be a positive Telegram user id")

    threshold = _parse_float("TRIAGE_THRESHOLD", get("TRIAGE_THRESHOLD"), errors)
    # Overall publishability is 0-10; a note must score strictly ABOVE this to be drafted.
    if not 0.0 <= threshold <= 10.0:
        errors.append("TRIAGE_THRESHOLD must be between 0 and 10")

    min_words = _parse_int("TRIAGE_MIN_WORDS", get("TRIAGE_MIN_WORDS"), errors)
    if min_words < 1:
        errors.append("TRIAGE_MIN_WORDS must be at least 1")

    news_days = _parse_int("NEWS_MAX_AGE_DAYS", get("NEWS_MAX_AGE_DAYS"), errors)
    news_items = _parse_int("NEWS_MAX_ITEMS", get("NEWS_MAX_ITEMS"), errors)
    if news_days < 1 or news_items < 1:
        errors.append("NEWS_MAX_AGE_DAYS and NEWS_MAX_ITEMS must be at least 1")

    min_chars = _parse_int("DRAFT_MIN_CHARS", get("DRAFT_MIN_CHARS"), errors)
    max_chars = _parse_int("DRAFT_MAX_CHARS", get("DRAFT_MAX_CHARS"), errors)
    if not 0 < min_chars < max_chars:
        errors.append("DRAFT_MIN_CHARS must be positive and less than DRAFT_MAX_CHARS")

    public_url = env.get("PUBLIC_URL", "").strip().rstrip("/") or None
    webhook_secret = env.get("WEBHOOK_SECRET", "").strip() or None
    if public_url is not None:
        if not public_url.startswith("https://"):
            errors.append("PUBLIC_URL must start with https:// (Telegram webhooks require HTTPS)")
        if webhook_secret is None or not WEBHOOK_SECRET_RE.match(webhook_secret):
            errors.append("WEBHOOK_SECRET is required with PUBLIC_URL: 16-256 characters of A-Z, a-z, 0-9, _ or -")
    unclear_ratio = _parse_float("TRANSCRIPT_MAX_UNCLEAR_RATIO", get("TRANSCRIPT_MAX_UNCLEAR_RATIO"), errors)
    if not 0.0 <= unclear_ratio <= 1.0:
        errors.append("TRANSCRIPT_MAX_UNCLEAR_RATIO must be between 0 and 1")
    sweep_minutes = _parse_int("SWEEP_INTERVAL_MINUTES", get("SWEEP_INTERVAL_MINUTES"), errors)
    if sweep_minutes < 5:
        errors.append("SWEEP_INTERVAL_MINUTES must be at least 5")
    max_regenerations = _parse_int("MAX_REGENERATIONS", get("MAX_REGENERATIONS"), errors)
    if max_regenerations < 0:
        errors.append("MAX_REGENERATIONS must be 0 or more")
    port = _parse_int("PORT", get("PORT"), errors)
    if not 0 < port < 65536:
        errors.append("PORT must be between 1 and 65535")

    settings = Settings(
        telegram_bot_token=env["TELEGRAM_BOT_TOKEN"].strip(),
        telegram_chat_id=capture_id,
        telegram_review_chat_id=review_id,
        meera_user_id=meera_id,
        gemini_api_key=env["GEMINI_API_KEY"].strip(),
        triage_model=get("TRIAGE_MODEL"),
        draft_model=get("DRAFT_MODEL"),
        transcribe_model=get("TRANSCRIBE_MODEL"),
        db_path=Path(get("DB_PATH")),
        timezone=_parse_zone(get("TIMEZONE"), errors),
        triage_threshold=threshold,
        triage_min_words=min_words,
        news_max_age_days=news_days,
        news_max_items=news_items,
        draft_min_chars=min_chars,
        draft_max_chars=max_chars,
        public_url=public_url,
        webhook_secret=webhook_secret,
        port=port,
        transcript_max_unclear_ratio=unclear_ratio,
        qa_model=get("QA_MODEL"),
        sweep_interval_minutes=sweep_minutes,
        max_regenerations=max_regenerations,
    )
    if errors:
        raise ConfigError("Invalid configuration: " + "; ".join(errors))
    return settings


# Real environment variables win over .env, so the host's secret store is authoritative.
load_dotenv(override=False)
settings: Settings = load_settings(os.environ)


if __name__ == "__main__":
    print(settings)
