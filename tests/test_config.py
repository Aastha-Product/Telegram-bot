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
    assert s.sweep_interval_minutes == 60 and s.max_regenerations == 3
    assert s.qa_model == "gemini-3.5-flash" and s.transcript_max_unclear_ratio == 0.2
    assert s.timezone.key == "Asia/Kolkata"
    assert s.triage_threshold == 8.0
    assert s.triage_min_words == 12
    assert (s.draft_min_chars, s.draft_max_chars) == (200, 3000)
    assert (s.news_max_age_days, s.news_max_items) == (14, 2)


def test_overrides_are_applied() -> None:
    s = load_settings({**VALID_ENV, "DRAFT_MODEL": "gemini-3.8-flash", "SWEEP_INTERVAL_MINUTES": "15"})
    assert s.draft_model == "gemini-3.8-flash"
    assert s.sweep_interval_minutes == 15


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
        ("SWEEP_INTERVAL_MINUTES", "1"),
        ("TRANSCRIPT_MAX_UNCLEAR_RATIO", "1.5"),
        ("MAX_REGENERATIONS", "-1"),
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


def test_polling_mode_by_default() -> None:
    s = load_settings(VALID_ENV)
    assert not s.webhook_mode and s.public_url is None and s.port == 8080


def test_webhook_mode_requires_https_and_strong_secret() -> None:
    secret = "a" * 32
    s = load_settings({**VALID_ENV, "PUBLIC_URL": "https://bot.example.com/", "WEBHOOK_SECRET": secret, "PORT": "9000"})
    assert s.webhook_mode and s.public_url == "https://bot.example.com" and s.port == 9000
    assert secret not in repr(s)
    with pytest.raises(ConfigError, match="https"):
        load_settings({**VALID_ENV, "PUBLIC_URL": "http://bot.example.com", "WEBHOOK_SECRET": secret})
    for bad in ("", "short", "has spaces in it!!", "x" * 300):
        with pytest.raises(ConfigError, match="WEBHOOK_SECRET"):
            load_settings({**VALID_ENV, "PUBLIC_URL": "https://bot.example.com", "WEBHOOK_SECRET": bad})
    with pytest.raises(ConfigError, match="PORT"):
        load_settings({**VALID_ENV, "PORT": "70000"})


def test_threshold_uses_zero_to_ten_scale() -> None:
    assert load_settings({**VALID_ENV, "TRIAGE_THRESHOLD": "7.5"}).triage_threshold == 7.5
