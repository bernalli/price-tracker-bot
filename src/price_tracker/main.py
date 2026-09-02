"""Entry point — initializes DB, HTTP, scheduler, and starts the bot."""

from __future__ import annotations

import asyncio
import time
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
from price_tracker.db import apply_runtime_pragmas
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.notifier.digest import DigestService
from price_tracker.notifier.preferences import PreferencesManager
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
    # Alias used by Plan 2 F3.D notification handlers.
    application.bot_data["repository"] = repo
    # Alias used by ``bot.decorators._db`` and direct
    # ``context.bot_data["db"]`` lookups across handler modules
    # (product_io, history, product_list, monitoring, debug, callbacks/*).
    # Pre-refactor monolith stored the repository under ``"db"``; the
    # Plan 1 F1 split renamed the post_init key to ``"repo"`` but left the
    # handler-side lookups untouched, so this alias keeps them wired.
    application.bot_data["db"] = repo
    # Alias used by ``bot.decorators._scraper`` and direct
    # ``context.bot_data["scraper"]`` lookups (product, product_io,
    # monitoring, debug, callbacks/_menu, callbacks/_product). Same Plan 1
    # F1 naming drift: bootstrap stores the registry under ``"registry"``,
    # handlers expect ``"scraper"``.
    application.bot_data["scraper"] = application.bot_data["registry"]

    for uid in config.admin_users:
        await repo.ensure_user(user_id=uid, is_admin=True)

    application.bot_data["http_client"] = build_client(timeout=float(config.request_timeout))

    # Wire DigestService so /digest_now and other digest-driven flows can
    # reach it via context.bot_data.
    metrics: MetricsRegistry | None = application.bot_data.get("metrics")
    application.bot_data["digest_service"] = DigestService(
        repo=repo,
        bot=application.bot,
        metrics=metrics,
        lang=config.lang,
    )


async def _setup_scheduler(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    config: Config = application.bot_data["config"]
    repo: Repository = application.bot_data["repo"]
    client = application.bot_data["http_client"]
    registry: ScraperRegistry = application.bot_data["registry"]

    metrics: MetricsRegistry | None = application.bot_data.get("metrics")
    health_mgr = HealthManager(repo, metrics=metrics)
    await health_mgr.load()
    application.bot_data["health_manager"] = health_mgr

    # Prefs and digest must be wired here, not just on the bot_data service:
    # the periodic job is precisely the sender that mute, quiet hours and digest
    # mode exist to govern, and without them it bypassed all three.
    notifier = TelegramNotifier(
        application.bot,
        metrics=metrics,
        prefs=PreferencesManager(repo),
        digest=application.bot_data["digest_service"],
    )
    application.bot_data["scheduler"] = Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=client,
            notifier=notifier,
            max_consecutive_errors=config.max_consecutive_errors,
            listing_gone_confirmations=config.listing_gone_confirmations,
            delay_between_products=config.check_delay_seconds,
            notification_cooldown_hours=config.notification_cooldown_hours,
            read_confirmations=config.read_confirmations,
            lang=config.lang,
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


# How often the digest-flush job runs, and the fallback cadence for users with no
# stored digest_interval_minutes preference.
DIGEST_FLUSH_INTERVAL_SECONDS = 60
DIGEST_FLUSH_DEFAULT_MINUTES = 60


async def digest_flush_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Flush due per-user notification digests (Feature D).

    Without this scheduled job, enqueued digest entries were never delivered
    except via manual /digest_now — they piled up indefinitely (#25).
    """
    digest_service = context.bot_data.get("digest_service")
    if digest_service is None:
        return
    try:
        await digest_service.flush_due(interval_minutes=DIGEST_FLUSH_DEFAULT_MINUTES)
    except Exception:  # noqa: BLE001 — a flush failure must not kill the job
        log.exception("digest_flush_job failed")


async def bootstrap_database(database_path: str) -> aiosqlite.Connection:
    """Open the SQLite connection with the schema fully applied.

    Migrations must run here, before anything reads the database: `amain`
    reads `bot_config` (persisted check interval) while wiring the job queue,
    which happens BEFORE PTB's post_init — on a brand-new deployment that
    read used to crash with `no such table: bot_config`.
    """
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    db_conn = await aiosqlite.connect(database_path)
    db_conn.row_factory = aiosqlite.Row
    await apply_runtime_pragmas(db_conn)
    await apply_migrations(db_conn, MIGRATIONS_DIR)
    return db_conn


async def amain() -> None:
    config = Config.from_env()
    configure_logging(level=config.log_level)
    log.info("bot.starting", log_level=config.log_level)

    db_conn = await bootstrap_database(config.database_path)

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
    application.bot_data["start_time"] = time.monotonic()

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
        # Prefer a persisted interval (/intervallo writes bot_config) over the
        # env default, so a runtime change survives a restart.
        interval_minutes = config.check_interval_minutes
        cursor = await db_conn.execute(
            "SELECT value FROM bot_config WHERE key = ?", ("check_interval_minutes",)
        )
        row = await cursor.fetchone()
        if row and row[0] and str(row[0]).isdigit():
            interval_minutes = max(5, int(row[0]))
        application.job_queue.run_repeating(
            scheduled_check_job,
            interval=interval_minutes * 60,
            first=60,
            name="periodic_check",
        )
        application.job_queue.run_repeating(
            digest_flush_job,
            interval=DIGEST_FLUSH_INTERVAL_SECONDS,
            first=DIGEST_FLUSH_INTERVAL_SECONDS,
            name="digest_flush",
        )

    await application.initialize()
    # PTB ≥22 does not call ``post_init`` from ``initialize()`` — only
    # ``run_polling()``/``run_webhook()`` do. We use the manual
    # ``initialize()``+``start()``+``updater.start_polling()`` pattern to keep
    # the metrics server lifecycle outside PTB, so we must invoke the registered
    # post-init callback ourselves before starting the application.
    await _combined_post_init(application)
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
