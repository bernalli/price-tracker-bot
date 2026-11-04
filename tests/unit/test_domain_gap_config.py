"""``DOMAIN_MIN_GAP_SECONDS`` reaches the process-wide pacer at startup."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest

from price_tracker.config import Config
from price_tracker.core.pacer import (
    DEFAULT_MIN_GAP_SECONDS,
    DomainPacer,
    configure_domain_pacer,
    get_domain_pacer,
)
from price_tracker.core.registry import ScraperRegistry
from price_tracker.main import _combined_post_init
from tests.support.fake_telegram import FakeRequest, make_application

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def _restore_pacer() -> Iterator[None]:
    previous = get_domain_pacer()
    try:
        yield
    finally:
        configure_domain_pacer(previous)


def _config(monkeypatch: pytest.MonkeyPatch, value: str | None) -> Config:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    if value is None:
        monkeypatch.delenv("DOMAIN_MIN_GAP_SECONDS", raising=False)
    else:
        monkeypatch.setenv("DOMAIN_MIN_GAP_SECONDS", value)
    return Config.from_env()


def test_default_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _config(monkeypatch, None).domain_min_gap_seconds == DEFAULT_MIN_GAP_SECONDS


def test_blank_gap_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _config(monkeypatch, "  ").domain_min_gap_seconds == DEFAULT_MIN_GAP_SECONDS


@pytest.mark.parametrize(("raw", "expected"), [("0", 0.0), ("45", 45.0), ("2.5", 2.5)])
def test_gap_from_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: float) -> None:
    assert _config(monkeypatch, raw).domain_min_gap_seconds == expected


@pytest.mark.parametrize("raw", ["-1", "nan", "inf", "soon"])
def test_unusable_gap_fails_at_startup(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    with pytest.raises(ValueError, match="DOMAIN_MIN_GAP_SECONDS"):
        _config(monkeypatch, raw)


@pytest.mark.usefixtures("_restore_pacer")
async def test_post_init_installs_the_configured_pacer(tmp_path: Path) -> None:
    config = Config(
        telegram_bot_token="fake-token-for-test",  # noqa: S106 — test fixture
        admin_users=(),
        check_interval_minutes=360,
        database_path=str(tmp_path / "test.db"),
        default_threshold_type="percentage",
        default_threshold_value="10",
        max_consecutive_errors=10,
        check_delay_seconds=5.0,
        notification_cooldown_hours=24,
        request_timeout=30,
        log_level="INFO",
        lang="en",
        prometheus_bind="127.0.0.1:0",
        metrics_enabled=False,
        domain_min_gap_seconds=42.0,
    )
    configure_domain_pacer(DomainPacer(min_gap=0))
    db_conn = await aiosqlite.connect(config.database_path)
    db_conn.row_factory = aiosqlite.Row
    application = make_application(FakeRequest())
    application.bot_data["config"] = config
    application.bot_data["db_conn"] = db_conn
    application.bot_data["registry"] = ScraperRegistry()
    try:
        await _combined_post_init(application)
        assert get_domain_pacer().min_gap == 42.0
    finally:
        await application.bot_data["http_client"].aclose()
        await db_conn.close()
