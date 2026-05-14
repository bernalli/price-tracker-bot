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
