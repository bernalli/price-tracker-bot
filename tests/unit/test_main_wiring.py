"""Tests for the main.py post-init wiring.

Verifies that ``_combined_post_init`` populates ``bot_data["health_manager"]``
with a concrete ``HealthManager`` and that the scheduler shares the same
instance via ``SchedulerDeps.health_mgr`` (no silent no-op fallback).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from price_tracker.config import Config
from price_tracker.core.health import HealthManager
from price_tracker.core.registry import ScraperRegistry, discover_builtin_scrapers
from price_tracker.core.scheduler import Scheduler
from price_tracker.main import _combined_post_init
from tests.support.fake_telegram import FakeRequest, make_application

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

    Regression guard: prior code left the key unset, so ``/health`` would
    crash with KeyError in production.
    """
    import aiosqlite

    db_conn = await aiosqlite.connect(fake_config.database_path)
    db_conn.row_factory = aiosqlite.Row

    application = make_application(FakeRequest())
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


async def test_startup_runs_announcement_after_polling_and_cancels_on_shutdown(
    fake_config, memory_db, monkeypatch
):
    from price_tracker import main

    started = asyncio.Event()
    cancelled = asyncio.Event()
    events = []
    tasks = []

    async def broadcast(*args):
        events.append("broadcast")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    def create_task(coroutine, *, name=None):
        task = asyncio.create_task(coroutine, name=name)
        tasks.append(task)
        return task

    async def stop():
        assert cancelled.is_set()
        assert all(task.cancelled() for task in tasks)
        events.append("stop")

    application = Mock(
        bot_data={},
        bot=Mock(),
        initialize=AsyncMock(),
        start=AsyncMock(side_effect=lambda: events.append("start")),
        stop=AsyncMock(side_effect=stop),
        shutdown=AsyncMock(),
        updater=Mock(
            start_polling=AsyncMock(side_effect=lambda: events.append("polling")),
            stop=AsyncMock(),
        ),
        create_task=Mock(side_effect=create_task),
    )
    builder = Mock()
    builder.token.return_value.post_init.return_value.build.return_value = application
    monkeypatch.setattr(main, "Application", Mock(builder=Mock(return_value=builder)))
    monkeypatch.setattr(Config, "from_env", Mock(return_value=fake_config))
    monkeypatch.setattr(main, "bootstrap_database", AsyncMock(return_value=memory_db))
    monkeypatch.setattr(main, "configure_logging", Mock())
    monkeypatch.setattr(main, "discover_builtin_scrapers", Mock())
    monkeypatch.setattr(main, "discover_dropin_scrapers", Mock())
    monkeypatch.setattr(main, "register_handlers", Mock())
    monkeypatch.setattr(main, "build_client", Mock())
    monkeypatch.setattr(main, "sync_command_menus", AsyncMock())
    setup_scheduler = AsyncMock(side_effect=lambda app: events.append("scheduler"))
    monkeypatch.setattr(main, "_setup_scheduler", setup_scheduler)
    monkeypatch.setattr(main, "announce_release", broadcast)

    runner = asyncio.create_task(main.amain())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert events == ["scheduler", "start", "polling", "broadcast"]
        assert not runner.done()
        application.create_task.assert_called_once()
        assert not tasks[0].done()
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner, timeout=2)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert cancelled.is_set()
    assert events[-1] == "stop"
    application.updater.stop.assert_awaited_once()
    application.shutdown.assert_awaited_once()
