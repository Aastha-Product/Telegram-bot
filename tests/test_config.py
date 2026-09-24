from datetime import time
from pathlib import Path

import pytest

from config import REQUIRED_VARS, ConfigError, load_settings

VALID_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:SECRET_TOKEN_VALUE",
    "TELEGRAM_CHAT_ID": "-1001234567890",
    "TELEGRAM_REVIEW_CHAT_ID": "987654321",
    "MEERA_USER_ID": "987654321",
    "GEMINI_API_KEY": "SECRET_API_KEY_VALUE",
}


def test_loads_required_and_defaults() -> None:
    s = load_settings(VALID_ENV)
    assert s.telegram_chat_id == -1001234567890
    assert s.meera_user_id == 987654321
    assert s.triage_model == "gemini-3.5-flash-lite"
    assert s.draft_model == "gemini-3.5-flash"
    assert s.transcribe_model == "gemini-3.5-flash"
    assert s.db_path == Path("skinstinct.db")
    assert s.schedule_days == ("mon", "wed", "fri")
    assert s.schedule_time == time(7, 30)
    assert s.timezone.key == "Asia/Kolkata"
    assert s.triage_threshold == 6.0
    assert s.triage_min_words == 12
    assert (s.draft_min_chars, s.draft_max_chars) == (200, 3000)
    assert (s.news_max_age_days, s.news_max_items) == (14, 2)


def test_overrides_are_applied() -> None:
    s = load_settings({**VALID_ENV, "DRAFT_MODEL": "gemini-3.8-flash", "SCHEDULE_DAYS": "Tue, Thu"})
    assert s.draft_model == "gemini-3.8-flash"
    assert s.schedule_days == ("tue", "thu")


@pytest.mark.parametrize("name", REQUIRED_VARS)
def test_missing_required_var_is_named(name: str) -> None:
    env = {k: v for k, v in VALID_ENV.items() if k != name}
    with pytest.raises(ConfigError, match=name):
        load_settings(env)


def test_blank_required_var_counts_as_missing() -> None:
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        load_settings({**VALID_ENV, "GEMINI_API_KEY": "   "})


def test_all_missing_vars_listed_together() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings({})
    for name in REQUIRED_VARS:
        assert name in str(exc.value)


def test_capture_chat_id_must_be_channel_id() -> None:
    with pytest.raises(ConfigError, match="-100"):
        load_settings({**VALID_ENV, "TELEGRAM_CHAT_ID": "123456"})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MEERA_USER_ID", "not-a-number"),
        ("TRIAGE_THRESHOLD", "11"),
        ("TRIAGE_THRESHOLD", "-1"),
        ("TRIAGE_MIN_WORDS", "0"),
        ("SCHEDULE_DAYS", "monday"),
        ("SCHEDULE_TIME", "7.30am"),
        ("TIMEZONE", "Mars/Olympus"),
        ("DRAFT_MIN_CHARS", "5000"),
        ("NEWS_MAX_ITEMS", "0"),
    ],
)
def test_invalid_values_rejected(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings({**VALID_ENV, name: value})


def test_repr_masks_secrets() -> None:
    text = repr(load_settings(VALID_ENV))
    assert "SECRET_TOKEN_VALUE" not in text
    assert "SECRET_API_KEY_VALUE" not in text
    assert "telegram_bot_token='***'" in text
    assert "gemini_api_key='***'" in text
    assert str(load_settings(VALID_ENV)) == text


def test_error_messages_never_contain_secret_values() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings({**VALID_ENV, "TELEGRAM_CHAT_ID": "oops"})
    assert "SECRET" not in str(exc.value)


def test_threshold_uses_zero_to_ten_scale() -> None:
    assert load_settings({**VALID_ENV, "TRIAGE_THRESHOLD": "7.5"}).triage_threshold == 7.5
