"""Regression: ``main.amain()`` must invoke ``_combined_post_init`` explicitly.

python-telegram-bot ≥22 no longer runs ``post_init`` from
:py:meth:`Application.initialize`; it is only invoked by
:py:meth:`Application.run_polling` / :py:meth:`Application.run_webhook`.
``main.py`` uses the manual ``initialize/start/start_polling`` pattern to keep
the metrics server lifecycle outside PTB, so it must call the callback
explicitly. Without that call, ``bot_data["scheduler"]`` remains unset and the
scheduled ``price_check`` job raises ``KeyError: 'scheduler'`` every interval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest
from telegram import Bot
from telegram.ext import Application, Updater

from price_tracker.config import Config
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
async def test_initialize_alone_does_not_wire_scheduler(
    fake_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Application.initialize()`` alone leaves ``bot_data['scheduler']`` unset.

    Confirms the PTB ≥22 quirk that motivates the explicit
    ``await _combined_post_init(application)`` call in ``main.amain()``. If this
    test ever fails (scheduler key present after initialize alone), the upstream
    behaviour has changed and the explicit call in ``main.py`` can be removed.
    """

    async def _noop(self: object) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(Bot, "initialize", _noop)
    monkeypatch.setattr(Bot, "shutdown", _noop)
    monkeypatch.setattr(Updater, "initialize", _noop)
    monkeypatch.setattr(Updater, "shutdown", _noop)

    db_conn = await aiosqlite.connect(":memory:")
    db_conn.row_factory = aiosqlite.Row

    application = (
        Application.builder()
        .token(fake_config.telegram_bot_token)
        .post_init(_combined_post_init)
        .build()
    )
    application.bot_data["config"] = fake_config
    application.bot_data["db_conn"] = db_conn

    registry = ScraperRegistry()
    discover_builtin_scrapers(registry)
    application.bot_data["registry"] = registry

    try:
        await application.initialize()
        assert "scheduler" not in application.bot_data, (
            "PTB now invokes post_init from initialize() — the explicit call in "
            "main.amain() can be removed and this regression guard updated."
        )
    finally:
        await application.shutdown()
        await db_conn.close()


@pytest.mark.asyncio
async def test_post_init_populates_all_handler_lookup_keys(
    fake_config: Config,
) -> None:
    """Regression: every key looked up by bot decorators / handlers must be
    populated by ``_combined_post_init``.

    Splitting the bot.py monolith introduced naming drift between bootstrap
    (sets ``bot_data["repo"]``, ``["registry"]``) and handler code (looks up
    ``bot_data["db"]``, ``["scraper"]``). Without these aliases, every
    command going through ``bot.decorators._db`` / ``_scraper`` crashes with
    ``KeyError`` and the Telegram user sees the generic
    ``"❌ Si è verificato un errore. Riprova tra qualche istante."``.
    """

    db_conn = await aiosqlite.connect(":memory:")
    db_conn.row_factory = aiosqlite.Row

    application = (
        Application.builder()
        .token(fake_config.telegram_bot_token)
        .post_init(_combined_post_init)
        .build()
    )
    application.bot_data["config"] = fake_config
    application.bot_data["db_conn"] = db_conn

    registry = ScraperRegistry()
    discover_builtin_scrapers(registry)
    application.bot_data["registry"] = registry

    try:
        await _combined_post_init(application)

        required = {
            "config",
            "db_conn",
            "repo",
            "repository",
            "db",
            "registry",
            "scraper",
            "http_client",
            "digest_service",
            "health_manager",
            "scheduler",
        }
        missing = required - set(application.bot_data.keys())
        assert not missing, (
            f"bot_data missing keys after post_init — handlers will KeyError: {missing}"
        )
    finally:
        http_client = application.bot_data.get("http_client")
        if http_client is not None:
            await http_client.aclose()
        await db_conn.close()


@pytest.mark.asyncio
async def test_explicit_post_init_after_initialize_wires_scheduler(
    fake_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors ``main.amain()`` startup sequence and asserts wiring is complete.

    Invokes ``Application.initialize()`` followed by an explicit
    ``await _combined_post_init(application)`` — the production fix. After this
    pair, ``bot_data['scheduler']`` must hold a concrete ``Scheduler`` instance.
    """

    async def _noop(self: object) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(Bot, "initialize", _noop)
    monkeypatch.setattr(Bot, "shutdown", _noop)
    monkeypatch.setattr(Updater, "initialize", _noop)
    monkeypatch.setattr(Updater, "shutdown", _noop)

    db_conn = await aiosqlite.connect(":memory:")
    db_conn.row_factory = aiosqlite.Row

    application = (
        Application.builder()
        .token(fake_config.telegram_bot_token)
        .post_init(_combined_post_init)
        .build()
    )
    application.bot_data["config"] = fake_config
    application.bot_data["db_conn"] = db_conn

    registry = ScraperRegistry()
    discover_builtin_scrapers(registry)
    application.bot_data["registry"] = registry

    try:
        await application.initialize()
        await _combined_post_init(application)

        assert "scheduler" in application.bot_data
        assert isinstance(application.bot_data["scheduler"], Scheduler)
    finally:
        http_client = application.bot_data.get("http_client")
        if http_client is not None:
            await http_client.aclose()
        await application.shutdown()
        await db_conn.close()


@pytest.mark.asyncio
async def test_post_init_seeds_products_tracked_gauge(fake_config: Config) -> None:
    """Startup must seed products_tracked from the DB instead of leaving the gauge at 0."""
    from decimal import Decimal  # noqa: PLC0415

    from prometheus_client import CollectorRegistry  # noqa: PLC0415

    from price_tracker.db.migrator import apply_migrations  # noqa: PLC0415
    from price_tracker.db.repository import Repository  # noqa: PLC0415
    from price_tracker.main import MIGRATIONS_DIR  # noqa: PLC0415
    from price_tracker.observability.metrics import MetricsRegistry  # noqa: PLC0415

    db_conn = await aiosqlite.connect(":memory:")
    db_conn.row_factory = aiosqlite.Row
    await apply_migrations(db_conn, MIGRATIONS_DIR)
    seed_repo = Repository(db_conn)
    await seed_repo.ensure_user(1, is_admin=True)
    await seed_repo.add_product(
        user_id=1,
        url="https://shop.example/a",
        name="A",
        domain="shop.example",
        initial_price=Decimal("10"),
        currency="EUR",
    )
    paused_id = await seed_repo.add_product(
        user_id=1,
        url="https://shop.example/b",
        name="B",
        domain="shop.example",
        initial_price=Decimal("20"),
        currency="EUR",
    )
    await seed_repo.pause_product(paused_id)

    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    application = (
        Application.builder()
        .token(fake_config.telegram_bot_token)
        .post_init(_combined_post_init)
        .build()
    )
    application.bot_data["config"] = fake_config
    application.bot_data["db_conn"] = db_conn
    application.bot_data["metrics"] = metrics
    registry = ScraperRegistry()
    discover_builtin_scrapers(registry)
    application.bot_data["registry"] = registry

    try:
        await _combined_post_init(application)
        assert application.bot_data["products_tracked"] == 1
        assert reg.get_sample_value("price_tracker_products_tracked_total") == 1.0
    finally:
        http_client = application.bot_data.get("http_client")
        if http_client is not None:
            await http_client.aclose()
        await db_conn.close()


@pytest.mark.asyncio
async def test_amain_closes_http_client_on_shutdown(
    fake_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown must close the store HTTP client created in post_init."""
    import asyncio  # noqa: PLC0415
    from typing import Any  # noqa: PLC0415
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from prometheus_client import CollectorRegistry  # noqa: PLC0415

    from price_tracker import main as main_mod  # noqa: PLC0415
    from price_tracker.core.http_client import build_store_client  # noqa: PLC0415
    from price_tracker.observability.metrics import MetricsRegistry  # noqa: PLC0415

    close_spy = AsyncMock()

    def _spied_build(*, timeout: float = 30.0) -> object:
        client = build_store_client(timeout=timeout)
        real_close = client.aclose

        async def _close() -> None:
            await close_spy()
            await real_close()

        client.aclose = _close  # type: ignore[method-assign]
        return client

    async def _noop(self: object, *args: object, **kwargs: object) -> None:  # noqa: ARG001
        return None

    async def _fake_start(self: Any) -> None:
        self._running = True

    async def _fake_stop(self: Any) -> None:
        self._running = False

    async def _noop_fn(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        return None

    async def _cancel_wait(self: object) -> None:  # noqa: ARG001
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: fake_config))
    monkeypatch.setattr(main_mod, "configure_logging", lambda *, level: None)
    monkeypatch.setattr(
        main_mod, "MetricsRegistry", lambda: MetricsRegistry(registry=CollectorRegistry())
    )
    monkeypatch.setattr(main_mod, "build_store_client", _spied_build)
    monkeypatch.setattr(main_mod, "sync_command_menus", _noop_fn)
    monkeypatch.setattr(main_mod, "announce_release", _noop_fn)
    monkeypatch.setattr(Application, "initialize", _noop)
    monkeypatch.setattr(Application, "start", _fake_start)
    monkeypatch.setattr(Application, "stop", _fake_stop)
    monkeypatch.setattr(Application, "shutdown", _noop)
    monkeypatch.setattr(Updater, "start_polling", _noop)
    monkeypatch.setattr(Updater, "stop", _noop)
    monkeypatch.setattr(asyncio.Event, "wait", _cancel_wait)

    with pytest.raises(asyncio.CancelledError):
        await main_mod.amain()

    close_spy.assert_awaited_once()
