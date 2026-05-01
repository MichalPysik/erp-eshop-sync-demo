"""Test settings – overrides the default database to use in-memory SQLite."""

from core.settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Never mock the e-shop in tests – use responses library for HTTP mocking instead
MOCK_ESHOP = False
