"""Tests for the main.py post-init wiring.

Verifies that ``_combined_post_init`` populates ``bot_data["health_manager"]``
with a concrete ``HealthManager`` and that the scheduler shares the same
instance via ``SchedulerDeps.health_mgr`` (no silent no-op fallback).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from telegram.ext import Application

from price_tracker.config import Config
from price_tracker.core.health import HealthManager
from price_tracker.core.registry import ScraperRegistry, discover_builtin_scrapers
from price_tracker.core.scheduler import Scheduler
from price_tracker.main import _combined_post_init

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_config(tmp_path: Path) -> Config:
    return Config(
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
    )


@pytest.mark.asyncio
async def test_post_init_wires_health_manager_into_bot_data(
    fake_config: Config,
) -> None:
    """``_combined_post_init`` must populate ``bot_data["health_manager"]``.

    Regression guard for the item 9 reviewer note: prior code left the key
    unset, so ``/health`` would crash with KeyError in production.
    """
    import aiosqlite

    db_conn = await aiosqlite.connect(fake_config.database_path)
    db_conn.row_factory = aiosqlite.Row

    application = Application.builder().token(fake_config.telegram_bot_token).build()
    application.bot_data["config"] = fake_config
    application.bot_data["db_conn"] = db_conn

    registry = ScraperRegistry()
    discover_builtin_scrapers(registry)
    application.bot_data["registry"] = registry

    try:
        await _combined_post_init(application)

        health_mgr = application.bot_data["health_manager"]
        assert isinstance(health_mgr, HealthManager)

        scheduler = application.bot_data["scheduler"]
        assert isinstance(scheduler, Scheduler)
        assert scheduler.deps.health_mgr is health_mgr
    finally:
        await application.bot_data["http_client"].aclose()
        await db_conn.close()
