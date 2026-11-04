"""``MIN_DROP_PERCENT`` and ``MIN_DROP_ABSOLUTE`` reach the scheduler at startup."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from price_tracker.config import Config
from price_tracker.core.drop_floor import DropFloor
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler
from price_tracker.main import _combined_post_init
from tests.support.fake_telegram import FakeRequest, make_application

if TYPE_CHECKING:
    from pathlib import Path


def _config(monkeypatch: pytest.MonkeyPatch, **env: str | None) -> Config:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return Config.from_env()


def test_default_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(monkeypatch, MIN_DROP_PERCENT=None, MIN_DROP_ABSOLUTE=None)
    assert (config.min_drop_percent, config.min_drop_absolute) == (Decimal(2), Decimal(1))


def test_floor_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(monkeypatch, MIN_DROP_PERCENT="0", MIN_DROP_ABSOLUTE="2.5")
    assert (config.min_drop_percent, config.min_drop_absolute) == (Decimal(0), Decimal("2.5"))


@pytest.mark.parametrize("name", ["MIN_DROP_PERCENT", "MIN_DROP_ABSOLUTE"])
@pytest.mark.parametrize("raw", ["-1", "nan", "inf", "soon"])
def test_unusable_floor_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, raw: str
) -> None:
    with pytest.raises(ValueError, match=name):
        _config(monkeypatch, **{name: raw})


async def test_post_init_gives_the_scheduler_the_configured_floor(tmp_path: Path) -> None:
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
        min_drop_percent=Decimal(3),
        min_drop_absolute=Decimal("0.5"),
    )
    db_conn = await aiosqlite.connect(config.database_path)
    db_conn.row_factory = aiosqlite.Row
    application = make_application(FakeRequest())
    application.bot_data["config"] = config
    application.bot_data["db_conn"] = db_conn
    application.bot_data["registry"] = ScraperRegistry()
    try:
        await _combined_post_init(application)
        scheduler = application.bot_data["scheduler"]
        assert isinstance(scheduler, Scheduler)
        assert scheduler.deps.drop_floor == DropFloor(Decimal(3), Decimal("0.5"))
    finally:
        await application.bot_data["http_client"].aclose()
        await db_conn.close()
