"""Regression tests for alert features that were persisted but never acted on.

Three settings the bot happily accepted and then ignored:

* ``/target`` wrote ``products.target_price`` and nothing ever read it.
* Availability was scraped on every check and never stored, so a sold-out
  product looked live and a restock was never announced.
* A Telegram send that failed was still recorded as an alert sent, starting the
  24h cooldown on a message the user never received.
"""

from __future__ import annotations

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
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
BASE = Decimal("100.00")


class _ScriptedScraper(AbstractScraper):
    name = "scripted"
    priority = 100

    def __init__(self, responses: list[ProductInfo]) -> None:
        self._responses = responses
        self.calls = 0

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        info = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return info


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
        initial_price=BASE,
        currency="EUR",
    )
    await repo.update_price(pid, BASE)
    for _ in range(10):
        await repo.add_price_history(pid, BASE)
    try:
        yield repo, pid
    finally:
        await conn.close()


async def _run(
    repo: Repository, scraper: AbstractScraper, notifier: AsyncMock, times: int = 1
) -> None:
    registry = ScraperRegistry()
    registry.register(scraper)
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        for _ in range(times):
            await scheduler.run_check_for_user(user_id=1)


@pytest.mark.asyncio
async def test_target_price_triggers_alert_below_threshold(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A target reached by a move too small for the percentage threshold must alert.

    Default threshold is 10%. A drop of 100 -> 92 is only 8%, but it crosses a
    target of 95, which is exactly what the user asked to be told about.
    """
    repo, pid = repo_with_product
    await repo.set_target_price(pid, Decimal("95.00"))
    scraper = _ScriptedScraper([ProductInfo(name="Widget", price=Decimal("92.00"), currency="EUR")])
    notifier = AsyncMock()

    await _run(repo, scraper, notifier)

    notifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_target_not_reached_does_not_alert(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Above the target and below the threshold: still silent."""
    repo, pid = repo_with_product
    await repo.set_target_price(pid, Decimal("80.00"))
    scraper = _ScriptedScraper([ProductInfo(name="Widget", price=Decimal("97.00"), currency="EUR")])
    notifier = AsyncMock()

    await _run(repo, scraper, notifier)

    notifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_already_below_does_not_renotify(
    repo_with_product: tuple[Repository, int],
) -> None:
    """The target fires on the crossing, not on every check that stays under it."""
    repo, pid = repo_with_product
    await repo.set_target_price(pid, Decimal("95.00"))
    await repo.update_price(pid, Decimal("92.00"))
    scraper = _ScriptedScraper([ProductInfo(name="Widget", price=Decimal("92.00"), currency="EUR")])
    notifier = AsyncMock()

    await _run(repo, scraper, notifier)

    notifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_availability_is_persisted(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A sold-out listing must be recorded as unavailable, not left looking live."""
    repo, pid = repo_with_product
    scraper = _ScriptedScraper(
        [ProductInfo(name="Widget", price=BASE, currency="EUR", available=False)]
    )
    notifier = AsyncMock()

    await _run(repo, scraper, notifier)

    product = await repo.get_product(pid)
    assert product is not None
    assert product.is_available is False


@pytest.mark.asyncio
async def test_back_in_stock_notifies(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Coming back in stock is worth a message — it is why people track things."""
    repo, pid = repo_with_product
    scraper = _ScriptedScraper(
        [
            ProductInfo(name="Widget", price=BASE, currency="EUR", available=False),
            ProductInfo(name="Widget", price=BASE, currency="EUR", available=True),
        ]
    )
    notifier = AsyncMock()

    await _run(repo, scraper, notifier, times=2)

    notifier.assert_awaited_once()
    assert notifier.await_args is not None
    assert "stock" in notifier.await_args.args[1].lower()
    product = await repo.get_product(pid)
    assert product is not None
    assert product.is_available is True


@pytest.mark.asyncio
async def test_failed_delivery_does_not_start_cooldown(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A message Telegram refused must not silence the next 24 hours of alerts.

    The notifier reports non-delivery by returning False; recording the alert as
    sent anyway would suppress every equal-or-smaller drop until the cooldown
    expired, for a message the user never saw.
    """
    repo, pid = repo_with_product
    scraper = _ScriptedScraper([ProductInfo(name="Widget", price=Decimal("80.00"), currency="EUR")])
    notifier = AsyncMock(return_value=False)

    await _run(repo, scraper, notifier)

    notifier.assert_awaited_once()
    product = await repo.get_product(pid)
    assert product is not None
    assert product.last_notified_at is None
