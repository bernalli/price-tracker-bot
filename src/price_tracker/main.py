"""Entry point — initializes DB, HTTP, scheduler, and starts the bot."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import httpx  # noqa: F401  — kept for future direct use; build_client returns AsyncClient
import structlog
from telegram.ext import Application, ContextTypes

from price_tracker.bot.handlers import register_handlers
from price_tracker.config import Config, parse_bind
from price_tracker.core.health import HealthManager
from price_tracker.core.http_client import build_client
from price_tracker.core.registry import (
    ScraperRegistry,
    discover_builtin_scrapers,
    discover_dropin_scrapers,
)
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.notifier.telegram import TelegramNotifier
from price_tracker.observability.logging import configure_logging
from price_tracker.observability.metrics import MetricsRegistry, MetricsServer

MIGRATIONS_DIR = Path(__file__).parent / "db" / "migrations"
PLUGIN_DIR_DEFAULT = Path("/app/plugins")

log = structlog.get_logger(__name__)


async def post_init(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    config: Config = application.bot_data["config"]
    db_conn: aiosqlite.Connection = application.bot_data["db_conn"]

    await apply_migrations(db_conn, MIGRATIONS_DIR)
    repo = Repository(db_conn)
    application.bot_data["repo"] = repo

    for uid in config.admin_users:
        await repo.ensure_user(user_id=uid, is_admin=True)

    application.bot_data["http_client"] = build_client(timeout=float(config.request_timeout))


async def _setup_scheduler(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    config: Config = application.bot_data["config"]
    repo: Repository = application.bot_data["repo"]
    client = application.bot_data["http_client"]
    registry: ScraperRegistry = application.bot_data["registry"]

    metrics: MetricsRegistry | None = application.bot_data.get("metrics")
    health_mgr = HealthManager(repo, metrics=metrics)
    await health_mgr.load()
    application.bot_data["health_manager"] = health_mgr

    notifier = TelegramNotifier(application.bot)
    application.bot_data["scheduler"] = Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=client,
            notifier=notifier,
            max_consecutive_errors=config.max_consecutive_errors,
            delay_between_products=config.check_delay_seconds,
            health_mgr=health_mgr,
            metrics=metrics,
        )
    )


async def _combined_post_init(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    await post_init(application)
    await _setup_scheduler(application)


async def scheduled_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    scheduler: Scheduler = context.application.bot_data["scheduler"]
    await scheduler.run_check_all()


async def amain() -> None:
    config = Config.from_env()
    configure_logging(level=config.log_level)
    log.info("bot.starting", log_level=config.log_level)

    Path(config.database_path).parent.mkdir(parents=True, exist_ok=True)
    db_conn = await aiosqlite.connect(config.database_path)
    db_conn.row_factory = aiosqlite.Row

    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .post_init(_combined_post_init)
        .build()
    )

    application.bot_data["config"] = config
    application.bot_data["db_conn"] = db_conn

    metrics = MetricsRegistry()
    application.bot_data["metrics"] = metrics

    metrics_server: MetricsServer | None = None
    if config.metrics_enabled:
        host, port = parse_bind(config.prometheus_bind)
        metrics_server = MetricsServer(host=host, port=port, metrics=metrics)
        await metrics_server.start()
        log.info("metrics_server.start", host=host, port=port)
    else:
        log.info("metrics_server.disabled")

    registry = ScraperRegistry()
    discover_builtin_scrapers(registry)
    discover_dropin_scrapers(registry, PLUGIN_DIR_DEFAULT)
    application.bot_data["registry"] = registry

    register_handlers(application)

    if application.job_queue:
        application.job_queue.run_repeating(
            scheduled_check_job,
            interval=config.check_interval_minutes * 60,
            first=60,
        )

    await application.initialize()
    await application.start()
    if application.updater is None:
        raise RuntimeError("Updater not initialized")
    await application.updater.start_polling()
    try:
        await asyncio.Event().wait()
    finally:
        if metrics_server is not None:
            await metrics_server.stop()
            log.info("metrics_server.stop")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await db_conn.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
