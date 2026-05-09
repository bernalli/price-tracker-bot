"""Integration test for scheduled price check (no real network)."""

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


class _StubScraper(AbstractScraper):
    name = "stub"
    priority = 100

    def __init__(self, response: ProductInfo) -> None:
        self._response = response
        self.calls = 0

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        self.calls += 1
        return self._response


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
        initial_price=Decimal("100"),
        currency="EUR",
    )
    try:
        yield repo, pid
    finally:
        await conn.close()


@pytest.mark.asyncio()
async def test_scheduler_updates_price_on_drop(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
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
        await scheduler.run_check_for_user(user_id=1)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.current_price == Decimal("80")
    assert stub.calls == 1


@pytest.mark.asyncio()
async def test_scheduler_increments_errors_on_scrape_failure(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(error="HTTP 429"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
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
        await scheduler.run_check_for_user(user_id=1)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 1


@pytest.mark.asyncio()
async def test_scheduler_triggers_alert_on_threshold_drop(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
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
        await scheduler.run_check_for_user(user_id=1)
    notifier.assert_awaited_once()
