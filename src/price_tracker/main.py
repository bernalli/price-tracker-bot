"""Entry point — initializes DB, HTTP, scheduler, and starts the bot."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiosqlite
import httpx  # noqa: F401  — kept for future direct use; build_client returns AsyncClient
from telegram.ext import Application, ContextTypes

from price_tracker.bot.handlers import register_handlers
from price_tracker.config import Config
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

MIGRATIONS_DIR = Path(__file__).parent / "db" / "migrations"
PLUGIN_DIR_DEFAULT = Path("/app/plugins")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def post_init(application: Application) -> None:
    config: Config = application.bot_data["config"]
    db_conn: aiosqlite.Connection = application.bot_data["db_conn"]

    await apply_migrations(db_conn, MIGRATIONS_DIR)
    repo = Repository(db_conn)
    application.bot_data["repo"] = repo

    for uid in config.admin_users:
        await repo.ensure_user(user_id=uid, is_admin=True)

    application.bot_data["http_client"] = build_client(timeout=float(config.request_timeout))


async def _setup_scheduler(application: Application) -> None:
    config: Config = application.bot_data["config"]
    repo: Repository = application.bot_data["repo"]
    client = application.bot_data["http_client"]
    registry: ScraperRegistry = application.bot_data["registry"]
    notifier = TelegramNotifier(application.bot)
    application.bot_data["scheduler"] = Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=client,
            notifier=notifier,
            max_consecutive_errors=config.max_consecutive_errors,
            delay_between_products=config.check_delay_seconds,
        )
    )


async def _combined_post_init(application: Application) -> None:
    await post_init(application)
    await _setup_scheduler(application)


async def scheduled_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    scheduler: Scheduler = context.application.bot_data["scheduler"]
    await scheduler.run_check_all()


async def amain() -> None:
    config = Config.from_env()
    setup_logging(config.log_level)

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
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await db_conn.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
