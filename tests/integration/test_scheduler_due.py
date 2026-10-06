"""The periodic check reads a product only when its own interval has elapsed.

`check_interval_minutes` was stored by `/refresh` but never read: the periodic
job checked every active product on the global cadence. The job now ticks every
`CHECK_TICK_MINUTES` and `run_check_due` picks the products whose interval (their
own, else the global one) has elapsed since their last attempt, successful or not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import CHECK_TICK_MINUTES, Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
GLOBAL_MINUTES = 360


class _RecordingScraper(AbstractScraper):
    name = "recording"
    priority = 100

    def __init__(self, *, fail: bool = False) -> None:
        self.urls: list[str] = []
        self.fail = fail

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        self.urls.append(url)
        if self.fail:
            return ProductInfo(error="price not found")
        return ProductInfo(name="Widget", price=Decimal("100"), currency="EUR")


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(user_id=1)
    try:
        yield repository
    finally:
        await conn.close()


async def _add(repo: Repository, n: int, *, interval: int | None = None) -> int:
    pid = await repo.add_product(
        user_id=1,
        url=f"https://example.com/p/{n}",
        name=f"Widget {n}",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    if interval is not None:
        await repo.set_check_interval(pid, interval)
    return pid


async def _set_checked(repo: Repository, pid: int, *, minutes_ago: int) -> None:
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    await repo._conn.execute("UPDATE products SET last_checked_at = ? WHERE id = ?", (stamp, pid))
    await repo._conn.commit()


def _scheduler(
    repo: Repository, scraper: _RecordingScraper, client: httpx.AsyncClient
) -> Scheduler:
    registry = ScraperRegistry()
    registry.register(scraper)
    return Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=client,
            notifier=AsyncMock(),
            delay_between_products=0,
        )
    )


async def test_never_checked_products_are_due(repo: Repository) -> None:
    await _add(repo, 1)
    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, scraper, client).run_check_due(
            global_interval_minutes=GLOBAL_MINUTES
        )

    assert scraper.urls == ["https://example.com/p/1"]


async def test_a_product_interval_overrides_the_global_one(repo: Repository) -> None:
    fast = await _add(repo, 1, interval=30)
    slow = await _add(repo, 2)
    await _set_checked(repo, fast, minutes_ago=40)
    await _set_checked(repo, slow, minutes_ago=40)
    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, scraper, client).run_check_due(
            global_interval_minutes=GLOBAL_MINUTES
        )

    assert scraper.urls == ["https://example.com/p/1"]


async def test_a_longer_product_interval_holds_back_a_globally_due_product(
    repo: Repository,
) -> None:
    pid = await _add(repo, 1, interval=720)
    await _set_checked(repo, pid, minutes_ago=400)
    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, scraper, client).run_check_due(
            global_interval_minutes=GLOBAL_MINUTES
        )

    assert scraper.urls == []


async def test_a_product_within_half_a_tick_of_its_interval_is_due(repo: Repository) -> None:
    pid = await _add(repo, 1, interval=60)
    await _set_checked(repo, pid, minutes_ago=60 - CHECK_TICK_MINUTES // 2)
    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, scraper, client).run_check_due(
            global_interval_minutes=GLOBAL_MINUTES
        )

    assert scraper.urls == ["https://example.com/p/1"]


async def test_a_failed_read_counts_as_an_attempt(repo: Repository) -> None:
    await _add(repo, 1, interval=60)
    scraper = _RecordingScraper(fail=True)
    async with httpx.AsyncClient() as client:
        scheduler = _scheduler(repo, scraper, client)
        await scheduler.run_check_due(global_interval_minutes=GLOBAL_MINUTES)
        await scheduler.run_check_due(global_interval_minutes=GLOBAL_MINUTES)

    assert scraper.urls == ["https://example.com/p/1"]


async def test_a_persisted_failure_counts_as_an_attempt_after_a_restart(
    repo: Repository,
) -> None:
    pid = await _add(repo, 1, interval=60)
    await repo.record_failure(pid, reason="parse_error", detail="price not found")
    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, scraper, client).run_check_due(
            global_interval_minutes=GLOBAL_MINUTES
        )

    assert scraper.urls == []


async def test_run_check_all_still_reads_every_active_product(repo: Repository) -> None:
    pid = await _add(repo, 1, interval=720)
    await _set_checked(repo, pid, minutes_ago=1)
    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, scraper, client).run_check_all()

    assert scraper.urls == ["https://example.com/p/1"]


# ── not-well-formed stored values ─────────────────────────────────────


async def _store_raw(repo: Repository, pid: int, column: str, value: object) -> None:
    assert column in {"check_interval_minutes", "last_checked_at", "last_error_at"}
    await repo._conn.execute(f"UPDATE products SET {column} = ? WHERE id = ?", (value, pid))
    await repo._conn.commit()


async def test_an_unreadable_timestamp_makes_only_its_product_due(repo: Repository) -> None:
    damaged = await _add(repo, 1)
    healthy = await _add(repo, 2)
    await _store_raw(repo, damaged, "last_checked_at", "not a timestamp")
    await _set_checked(repo, healthy, minutes_ago=1)
    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, scraper, client).run_check_due(
            global_interval_minutes=GLOBAL_MINUTES
        )

    assert scraper.urls == ["https://example.com/p/1"]


@pytest.mark.parametrize("stored", [0, -5, 10**15, "abc", 10**4 * 7])
async def test_an_unusable_product_interval_falls_back_to_the_global_one(
    repo: Repository, stored: object
) -> None:
    pid = await _add(repo, 1)
    await _store_raw(repo, pid, "check_interval_minutes", stored)
    await _set_checked(repo, pid, minutes_ago=40)
    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, scraper, client).run_check_due(
            global_interval_minutes=GLOBAL_MINUTES
        )

    assert scraper.urls == []


@pytest.mark.parametrize("global_minutes", [0, -60])
async def test_a_global_interval_below_the_tick_is_raised_to_the_tick(
    repo: Repository, global_minutes: int
) -> None:
    recent = await _add(repo, 1)
    older = await _add(repo, 2)
    await _set_checked(repo, recent, minutes_ago=1)
    await _set_checked(repo, older, minutes_ago=CHECK_TICK_MINUTES)
    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, scraper, client).run_check_due(
            global_interval_minutes=global_minutes
        )

    assert scraper.urls == ["https://example.com/p/2"]
