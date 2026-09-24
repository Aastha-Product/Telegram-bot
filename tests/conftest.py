"""Fake, obviously-not-real settings so `import config` works in tests without a .env."""

import os

_FAKE_ENV = {
    "TELEGRAM_BOT_TOKEN": "000000:FAKE_TOKEN_FOR_TESTS",
    "TELEGRAM_CHAT_ID": "-1000000000001",
    "TELEGRAM_REVIEW_CHAT_ID": "1",
    "MEERA_USER_ID": "1",
    "GEMINI_API_KEY": "FAKE_KEY_FOR_TESTS",
}

# Must run before any test module imports config (which validates at import time).
for _name, _value in _FAKE_ENV.items():
    os.environ.setdefault(_name, _value)
