"""Tests for Config.from_env loader."""

from __future__ import annotations

import pytest

from price_tracker.config import Config


def test_config_from_env_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing TELEGRAM_BOT_TOKEN raises ValueError (fail-fast)."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")  # explicit empty
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        Config.from_env()


def test_config_from_env_with_minimal_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only TELEGRAM_BOT_TOKEN required; everything else has sane defaults."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-abc123")
    monkeypatch.delenv("ALLOWED_USERS", raising=False)
    monkeypatch.delenv("CHECK_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    cfg = Config.from_env()
    assert cfg.telegram_bot_token == "test-token-abc123"  # noqa: S105 — test fixture
    assert cfg.admin_users == ()
    assert cfg.check_interval_minutes == 360
    assert cfg.database_path == "/data/pricetracker.db"
    assert cfg.default_threshold_type == "percentage"
    assert cfg.default_threshold_value == "10"
    assert cfg.max_consecutive_errors == 10
    assert cfg.check_delay_seconds == 5.0
    assert cfg.notification_cooldown_hours == 24
    assert cfg.request_timeout == 30
    assert cfg.log_level == "INFO"


def test_config_from_env_parses_admin_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """ALLOWED_USERS is a comma-separated list of integers."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALLOWED_USERS", "123, 456,789")
    cfg = Config.from_env()
    assert cfg.admin_users == (123, 456, 789)


def test_config_from_env_admin_users_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """ALLOWED_USERS empty or whitespace-only → empty tuple."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALLOWED_USERS", "  ,  ,")
    cfg = Config.from_env()
    assert cfg.admin_users == ()


def test_config_from_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each numeric env var should be parsed into the right typed field."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("CHECK_INTERVAL_MINUTES", "60")
    monkeypatch.setenv("DATABASE_PATH", "/tmp/test.db")  # noqa: S108 — test fixture
    monkeypatch.setenv("MAX_CONSECUTIVE_ERRORS", "3")
    monkeypatch.setenv("CHECK_DELAY_SECONDS", "1.5")
    monkeypatch.setenv("NOTIFICATION_COOLDOWN_HOURS", "6")
    monkeypatch.setenv("REQUEST_TIMEOUT", "10")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    cfg = Config.from_env()
    assert cfg.check_interval_minutes == 60
    assert cfg.database_path == "/tmp/test.db"  # noqa: S108
    assert cfg.max_consecutive_errors == 3
    assert cfg.check_delay_seconds == 1.5
    assert cfg.notification_cooldown_hours == 6
    assert cfg.request_timeout == 10
    assert cfg.log_level == "DEBUG"


def test_config_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config is a frozen dataclass — fields cannot be mutated after construction."""
    from dataclasses import FrozenInstanceError

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    cfg = Config.from_env()
    with pytest.raises(FrozenInstanceError):
        cfg.log_level = "ERROR"  # type: ignore[misc]
