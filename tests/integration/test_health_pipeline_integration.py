"""Regression tests: scheduler must call HealthManager pipeline helpers.

Bug observed in prod (2026-05-22): the ``scraper_health`` table stays empty
forever because :class:`Scheduler` only *consumes* HealthManager state
(``is_locked``/``is_half_open``) but never *produces* it via ``record_success``
or ``record_block``. Auto-quarantine therefore never engages — even a domain
returning HTTP 429 in a loop (Bug #1 xteink.com) would not be quarantined.

These tests exercise the full pipeline against a real Repository so that any
future regression that removes the pipeline hooks fails CI.
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

from price_tracker.core.exceptions import HTTPBlockStatus
from price_tracker.core.health import HealthManager, QuarantineState
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


class _SuccessScraper(AbstractScraper):
    name = "success-stub"
    priority = 100

    def __init__(self, price: Decimal) -> None:
        self._price = price

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        return ProductInfo(name="Widget", price=self._price, currency="EUR")


class _BlockingScraper(AbstractScraper):
    name = "blocking-stub"
    priority = 100

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        raise HTTPBlockStatus(status=429, url=url)


@pytest_asyncio.fixture
async def repo_with_product_example() -> AsyncIterator[tuple[Repository, int]]:
    """In-memory Repository with a single product on example.com."""
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


@pytest.mark.asyncio
async def test_scheduler_records_success_to_scraper_health(
    repo_with_product_example: tuple[Repository, int],
) -> None:
    """After a successful scrape, ``scraper_health`` must hold a CLOSED row
    for the product's domain with ``last_success_at`` populated.

    Regression: prior to the fix the scheduler called neither
    ``handle_success_in_pipeline`` nor ``health_mgr.record_success``, so the
    quarantine state machine never learned about successful scrapes and
    consecutive_blocks could never be reset after a HALF_OPEN probe.
    """
    repo, _pid = repo_with_product_example
    registry = ScraperRegistry()
    registry.register(_SuccessScraper(price=Decimal("90")))
    health_mgr = HealthManager(repo)
    await health_mgr.load()

    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                delay_between_products=0.0,
                health_mgr=health_mgr,
            )
        )
        await scheduler.run_check_for_user(user_id=1)

    row = await repo.get_scraper_health("example.com")
    assert row is not None, "scraper_health row missing — scheduler skipped record_success"
    assert row.state == QuarantineState.CLOSED.value
    assert row.consecutive_blocks == 0
    assert row.last_success_at is not None


@pytest.mark.asyncio
async def test_scheduler_records_block_to_scraper_health(
    repo_with_product_example: tuple[Repository, int],
) -> None:
    """When the scraper raises ``BlockEvent``, the scheduler must invoke
    ``handle_block_in_pipeline`` so consecutive_blocks increments.

    Regression: this is the actual mechanism that drives auto-quarantine after
    N consecutive failures (Bug #1 xteink loop). Without it, the
    ``is_locked``/``is_half_open`` checks in ``_run_tick`` always return False.
    """
    repo, _pid = repo_with_product_example
    registry = ScraperRegistry()
    registry.register(_BlockingScraper())
    health_mgr = HealthManager(repo)
    await health_mgr.load()

    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                delay_between_products=0.0,
                health_mgr=health_mgr,
            )
        )
        await scheduler.run_check_for_user(user_id=1)

    row = await repo.get_scraper_health("example.com")
    assert row is not None, "scraper_health row missing — scheduler skipped record_block"
    assert row.consecutive_blocks == 1
    assert row.last_block_at is not None
    assert row.last_block_reason == "HTTP 429"


@pytest.mark.asyncio
async def test_three_blocks_trigger_locked_t1(
    repo_with_product_example: tuple[Repository, int],
) -> None:
    """3 consecutive blocks on the same domain → state must be LOCKED_T1.

    End-to-end exercise of Bug #1 regression: the scheduler is what closes
    the loop by recording each block in HealthManager. After 3 blocks the
    state machine moves CLOSED → LOCKED_T1 and the next tick must skip the
    product entirely (verified by checking ``mgr.is_locked``).
    """
    repo, _pid = repo_with_product_example
    registry = ScraperRegistry()
    registry.register(_BlockingScraper())
    health_mgr = HealthManager(repo)
    await health_mgr.load()

    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=99,  # don't auto-disable; we want 3 hits
                delay_between_products=0.0,
                health_mgr=health_mgr,
            )
        )
        for _ in range(3):
            await scheduler.run_check_for_user(user_id=1)

    row = await repo.get_scraper_health("example.com")
    assert row is not None
    assert row.consecutive_blocks == 3
    assert row.state == QuarantineState.LOCKED_T1.value
    assert health_mgr.is_locked("example.com") is True
