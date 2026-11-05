"""Tests for the main.py post-init wiring.

Verifies that ``_combined_post_init`` populates ``bot_data["health_manager"]``
with a concrete ``HealthManager`` and that the scheduler shares the same
instance via ``SchedulerDeps.health_mgr`` (no silent no-op fallback).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
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
        assert application.create_task.call_count == 2
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


@pytest.mark.parametrize("custom", [False, True])
async def test_thumbnails_share_scheduler_spacing_and_repository(
    fake_config, memory_db, monkeypatch, tmp_path, custom
):
    from dataclasses import replace

    import httpx

    from price_tracker import main
    from price_tracker.bot.list_grid import GridRegistry
    from price_tracker.media.thumbnails import ThumbnailService

    cache_path = tmp_path / ("custom" if custom else "thumbs")
    config = replace(fake_config, image_cache_dir=str(cache_path) if custom else "")
    application = make_application(FakeRequest())
    application.bot_data.update(config=config, db_conn=memory_db, registry=ScraperRegistry())
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected network"))
    )
    monkeypatch.setattr(main, "build_client", lambda **kwargs: client)
    monkeypatch.setattr(main, "sync_command_menus", AsyncMock())
    try:
        await main._combined_post_init(application)
        service = application.bot_data["thumbnails"]
        scheduler = application.bot_data["scheduler"]
        assert isinstance(service, ThumbnailService)
        assert isinstance(application.bot_data["list_grids"], GridRegistry)
        assert scheduler.deps.thumbnails is service
        assert service._spacing is scheduler.deps.domain_spacing
        assert service._spacing is application.bot_data["domain_spacing"]
        assert service._spacing.min_gap == config.domain_min_gap_seconds
        assert service._cache.root == cache_path
        assert cache_path.is_dir()
        assert service._client is client
        assert service._referenced_urls == application.bot_data["repo"].list_image_urls
        assert list(await service._referenced_urls()) == []
    finally:
        await client.aclose()


@pytest.mark.parametrize("reason", ["disabled", "memory", "mkdir", "jpg", "zlib", "freetype2"])
async def test_thumbnail_setup_disables_both_services_together(
    fake_config, monkeypatch, tmp_path, reason
):
    from dataclasses import replace
    from types import SimpleNamespace

    from PIL import features

    from price_tracker import main
    from price_tracker.core.domain_spacing import DomainSpacing
    from price_tracker.media.cache import ThumbnailCache

    config = replace(
        fake_config,
        list_images=reason != "disabled",
        database_path=":memory:" if reason == "memory" else fake_config.database_path,
    )
    application: Any = SimpleNamespace(
        bot_data={
            "config": config,
            "repo": SimpleNamespace(list_image_urls=AsyncMock(return_value=[])),
            "http_client": object(),
            "domain_spacing": DomainSpacing(),
        }
    )
    warnings = Mock()
    monkeypatch.setattr(main.log, "warning", warnings)
    feature_check = Mock(side_effect=lambda name: name != reason)
    monkeypatch.setattr(features, "check", feature_check)
    ensure_dir = Mock(
        side_effect=PermissionError("cache unavailable") if reason == "mkdir" else None
    )
    monkeypatch.setattr(ThumbnailCache, "ensure_dir", ensure_dir)
    await main._setup_thumbnails(application)
    assert "thumbnails" not in application.bot_data
    assert "list_grids" not in application.bot_data
    assert not (tmp_path / "thumbs").exists()
    if reason in ("disabled", "memory"):
        feature_check.assert_not_called()
        warnings.assert_not_called()
    else:
        warnings.assert_called_once()
    if reason != "mkdir":
        ensure_dir.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
async def test_thumbnail_worker_is_cancelled_before_updater_stop(
    fake_config, memory_db, monkeypatch, tmp_path, enabled
):
    from dataclasses import replace

    from prometheus_client import CollectorRegistry

    from price_tracker import main
    from price_tracker.bot.list_grid import GridRegistry
    from price_tracker.core.domain_spacing import DomainSpacing
    from price_tracker.media.cache import ThumbnailCache
    from price_tracker.media.thumbnails import ThumbnailService
    from price_tracker.observability.metrics import MetricsRegistry

    config = replace(fake_config, list_images=enabled)
    running = asyncio.Event()
    cancelled = asyncio.Event()
    announced = asyncio.Event()
    tasks = []
    service = ThumbnailService(
        ThumbnailCache(tmp_path / "thumbs"),
        client=Mock(),
        job_pacing=DomainSpacing(),
        referenced_urls=AsyncMock(return_value=[]),
    )

    async def work():
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def announce(*args):
        announced.set()
        await asyncio.Event().wait()

    async def initialize(app):
        app.bot_data["repo"] = Mock()
        if enabled:
            app.bot_data.update(thumbnails=service, list_grids=GridRegistry())

    def create_task(coroutine, *, name):
        task = asyncio.create_task(coroutine, name=name)
        tasks.append(task)
        return task

    async def stop_polling():
        assert cancelled.is_set() is enabled
        assert all(task.cancelled() for task in tasks)

    application = Mock(
        bot_data={},
        initialize=AsyncMock(),
        start=AsyncMock(),
        stop=AsyncMock(),
        shutdown=AsyncMock(),
        updater=Mock(start_polling=AsyncMock(), stop=AsyncMock(side_effect=stop_polling)),
        create_task=Mock(side_effect=create_task),
    )
    builder = Mock()
    builder.token.return_value.post_init.return_value.build.return_value = application
    monkeypatch.setattr(main, "Application", Mock(builder=Mock(return_value=builder)))
    monkeypatch.setattr(Config, "from_env", Mock(return_value=config))
    monkeypatch.setattr(main, "bootstrap_database", AsyncMock(return_value=memory_db))
    monkeypatch.setattr(main, "_combined_post_init", initialize)
    monkeypatch.setattr(main, "configure_logging", Mock())
    monkeypatch.setattr(
        main, "MetricsRegistry", lambda: MetricsRegistry(registry=CollectorRegistry())
    )
    monkeypatch.setattr(main, "discover_builtin_scrapers", Mock())
    monkeypatch.setattr(main, "discover_dropin_scrapers", Mock())
    monkeypatch.setattr(main, "register_handlers", Mock())
    monkeypatch.setattr(main, "announce_release", announce)
    monkeypatch.setattr(service, "run", work)
    runner = asyncio.create_task(main.amain())
    try:
        await asyncio.wait_for(announced.wait(), timeout=2)
        if enabled:
            await asyncio.wait_for(running.wait(), timeout=2)
        assert application.create_task.call_count == 1 + int(enabled)
        assert {task.get_name() for task in tasks} == (
            {"release_announcement", "thumbnail_worker"} if enabled else {"release_announcement"}
        )
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner, timeout=2)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert cancelled.is_set() is enabled
    application.updater.stop.assert_awaited_once()
    application.shutdown.assert_awaited_once()
