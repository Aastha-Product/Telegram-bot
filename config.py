"""Typed settings loaded from the environment (and `.env` in local dev).

Import fails fast with a ConfigError naming every missing or invalid variable,
so the service never runs half-configured.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
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
    "SCHEDULE_DAYS": "mon,wed,fri",
    "SCHEDULE_TIME": "07:30",
    "TIMEZONE": "Asia/Kolkata",
    "TRIAGE_THRESHOLD": "6",
    "TRIAGE_MIN_WORDS": "12",
    "NEWS_MAX_AGE_DAYS": "14",
    "NEWS_MAX_ITEMS": "2",
    "DRAFT_MIN_CHARS": "200",
    "DRAFT_MAX_CHARS": "3000",
}

VALID_DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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
    schedule_days: tuple[str, ...]
    schedule_time: time
    timezone: ZoneInfo
    triage_threshold: float
    triage_min_words: int
    news_max_age_days: int
    news_max_items: int
    draft_min_chars: int
    draft_max_chars: int

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
            f"schedule_days={self.schedule_days}, "
            f"schedule_time={self.schedule_time.strftime('%H:%M')}, "
            f"timezone={self.timezone.key!r}, "
            f"triage_threshold={self.triage_threshold}, "
            f"triage_min_words={self.triage_min_words}, "
            f"news=[{self.news_max_age_days}d, {self.news_max_items} items], "
            f"draft_chars=[{self.draft_min_chars}, {self.draft_max_chars}])"
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


def _parse_days(raw: str, errors: list[str]) -> tuple[str, ...]:
    days = tuple(d.strip().lower() for d in raw.split(",") if d.strip())
    bad = [d for d in days if d not in VALID_DAYS]
    if not days or bad:
        errors.append(f"SCHEDULE_DAYS must be comma-separated from {', '.join(VALID_DAYS)}")
    return days


def _parse_time(raw: str, errors: list[str]) -> time:
    try:
        return time.fromisoformat(raw)
    except ValueError:
        errors.append("SCHEDULE_TIME must be HH:MM")
        return time(0, 0)


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
    # Triage scores notes 0-10 (Components Map answer key).
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
        schedule_days=_parse_days(get("SCHEDULE_DAYS"), errors),
        schedule_time=_parse_time(get("SCHEDULE_TIME"), errors),
        timezone=_parse_zone(get("TIMEZONE"), errors),
        triage_threshold=threshold,
        triage_min_words=min_words,
        news_max_age_days=news_days,
        news_max_items=news_items,
        draft_min_chars=min_chars,
        draft_max_chars=max_chars,
    )
    if errors:
        raise ConfigError("Invalid configuration: " + "; ".join(errors))
    return settings


# Real environment variables win over .env, so the host's secret store is authoritative.
load_dotenv(override=False)
settings: Settings = load_settings(os.environ)


if __name__ == "__main__":
    print(settings)
