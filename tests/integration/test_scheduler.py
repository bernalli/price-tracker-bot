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


class _RaisingScraper(AbstractScraper):
    """Scraper that raises an httpx error to exercise the run_check_for_user except branch."""

    name = "raising"
    priority = 100

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        raise httpx.ConnectError("simulated network failure")


@pytest.mark.asyncio()
async def test_scheduler_handles_scraper_exception_increments_errors(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Lines 55-57: httpx.HTTPError from scrape → increment_errors + log."""
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(_RaisingScraper())
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
async def test_scheduler_run_check_all_iterates_users(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Lines 62-64: run_check_all iterates list_active_users."""
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
        await scheduler.run_check_all()
    p = await repo.get_product(pid)
    assert p is not None
    assert p.current_price == Decimal("80")
    assert stub.calls == 1


@pytest.mark.asyncio()
async def test_scheduler_skips_when_no_scraper_resolves(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Lines 73-74: registry.resolve returns None → log + return."""
    repo, pid = repo_with_product
    registry = ScraperRegistry()  # empty registry → resolve(url) returns None
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
    # Price unchanged, no error counter increment (no exception raised)
    assert p.current_price is None or p.current_price == Decimal("100")


@pytest.mark.asyncio()
async def test_scheduler_outlier_price_rejected(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Lines 85-93: outlier read → log + return without updating price."""
    repo, pid = repo_with_product
    # Seed history: 6 points around 100 (>= MIN_HISTORY=5)
    for v in (100, 102, 98, 105, 100, 99):
        await repo.add_price_history(pid, Decimal(str(v)))

    # Scraper returns wildly inflated price (10x median) → outlier
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("1000"), currency="EUR"))
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
    # current_price NOT updated to 1000 (outlier rejected)
    assert p.current_price != Decimal("1000")
    # Notifier NOT called
    notifier.assert_not_awaited()


@pytest.mark.asyncio()
async def test_scheduler_first_check_no_old_price_no_alert() -> None:
    """Line 101: when old_price is None, return without crossing threshold check.

    Build a product with NULL initial_price to force old_price=None branch.
    """
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    pid = await repo.add_product(
        user_id=1,
        url="https://example.com/p/no-init",
        name="NoInit",
        domain="example.com",
        initial_price=None,
        currency="EUR",
    )
    try:
        stub = _StubScraper(ProductInfo(name="NoInit", price=Decimal("80"), currency="EUR"))
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
        notifier.assert_not_awaited()
        # Price WAS updated despite no alert
        p = await repo.get_product(pid)
        assert p is not None
        assert p.current_price == Decimal("80")
    finally:
        await conn.close()


@pytest.mark.asyncio()
async def test_scheduler_skips_inactive_product(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Line 69: _check_product returns early when product is inactive."""
    repo, pid = repo_with_product
    await repo.pause_product(pid)

    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80")))
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
        # Call _check_product directly: list_products_for_user(only_active=True)
        # would skip the inactive product upstream; we want to hit line 69.
        await scheduler._check_product(pid)
    assert stub.calls == 0


@pytest.mark.asyncio()
async def test_scheduler_price_none_increments_errors(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Lines 78-79: scraper returns price=None (e.g. parse failure) → increment_errors."""
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=None, error="parse failed"))
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
async def test_scheduler_cleanup_old_history(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Line 123: cleanup_old_history delegates to repo.delete_old_price_history."""
    repo, pid = repo_with_product
    registry = ScraperRegistry()
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
        deleted = await scheduler.cleanup_old_history(retention_days=365)
    # Empty history → 0 rows deleted
    assert deleted == 0
