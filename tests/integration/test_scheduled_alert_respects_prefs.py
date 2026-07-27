"""Scheduled price alerts must honour the user's notification preferences.

``TelegramNotifier.notify_alert`` implements mute, quiet hours, throttling and
digest routing, but the periodic scheduler reached the notifier through the
plain callable path, which sent immediately and consulted nothing. Every
preference the bot let the user set was therefore inert for exactly the alerts
those preferences exist to control.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import NotificationPrefs
from price_tracker.db.repository import Repository
from price_tracker.notifier.digest import DigestService
from price_tracker.notifier.preferences import PreferencesManager
from price_tracker.notifier.telegram import TelegramNotifier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


class _DropScraper(AbstractScraper):
    name = "drop"
    priority = 100

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        return ProductInfo(name="Widget", price=Decimal("80.00"), currency="EUR")


@pytest_asyncio.fixture
async def repo_with_product() -> AsyncIterator[tuple[Repository, int]]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    pid = await repo.add_product(
        user_id=1,
        url="https://example.com/p/1",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("100.00"),
        currency="EUR",
    )
    await repo.update_price(pid, Decimal("100.00"))
    for _ in range(10):
        await repo.add_price_history(pid, Decimal("100.00"))
    try:
        yield repo, pid
    finally:
        await conn.close()


async def _tick(repo: Repository, notifier: TelegramNotifier) -> None:
    registry = ScraperRegistry()
    registry.register(_DropScraper())
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)


@pytest.mark.asyncio
async def test_muted_user_gets_no_scheduled_alert(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, _pid = repo_with_product
    await repo.upsert_notification_prefs(NotificationPrefs(user_id=1, product_id=None, mute=True))
    bot = AsyncMock()
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo))

    await _tick(repo, notifier)

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_digest_user_gets_alert_queued_not_pushed(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    await repo.upsert_notification_prefs(
        NotificationPrefs(user_id=1, product_id=None, digest_mode=True)
    )
    bot = AsyncMock()
    notifier = TelegramNotifier(
        bot, prefs=PreferencesManager(repo), digest=DigestService(repo=repo, bot=bot)
    )

    await _tick(repo, notifier)

    bot.send_message.assert_not_awaited()
    pending = await repo.list_pending_digest(user_id=1)
    assert [e.product_id for e in pending] == [pid]


@pytest.mark.asyncio
async def test_unmuted_user_still_gets_the_rich_alert_text(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Routing through preferences must not downgrade the message body."""
    repo, _pid = repo_with_product
    bot = AsyncMock()
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo))

    await _tick(repo, notifier)

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "Price drop!" in text
    assert "80.00" in text


@pytest.mark.asyncio
async def test_digest_entry_renders_product_details(
    repo_with_product: tuple[Repository, int],
) -> None:
    """The queued payload must keep the structured fields the digest renders from."""
    repo, _pid = repo_with_product
    await repo.upsert_notification_prefs(
        NotificationPrefs(user_id=1, product_id=None, digest_mode=True)
    )
    bot = AsyncMock()
    digest = DigestService(repo=repo, bot=bot)
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo), digest=digest)

    await _tick(repo, notifier)
    await repo.upsert_notification_prefs(
        dataclasses.replace(
            NotificationPrefs(user_id=1, product_id=None, digest_mode=True), digest_mode=True
        )
    )
    flushed = await digest.flush_user(user_id=1)

    assert flushed == 1
    text = bot.send_message.await_args.kwargs["text"]
    assert "Widget" in text
    assert "80.00" in text
