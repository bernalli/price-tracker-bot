"""The periodic job never selects a product whose scraper's floor still holds.

A scraper that declares ``min_check_interval_minutes`` (Shopify: 360) is asked
about one product at most once per floor by the periodic job, whatever the
product's own or the global interval. The floor counts from the latest readable
anchor: this process's attempt, the stored read or failure, the creation.
Manual checks have no floor.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest_asyncio
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
FMT = "%Y-%m-%d %H:%M:%S"
FLOOR = 360


class _RecordingScraper(AbstractScraper):
    name = "recording"
    priority = 100
    min_check_interval_minutes: ClassVar[int] = FLOOR

    def __init__(self) -> None:
        self.urls: list[str] = []

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        self.urls.append(url)
        return ProductInfo(name="Widget", price=Decimal("100"), currency="EUR")


class _UnflooredScraper(_RecordingScraper):
    name = "unfloored"
    min_check_interval_minutes: ClassVar[int] = 0


async def _open_repo() -> tuple[aiosqlite.Connection, Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(user_id=1)
    return conn, repository


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn, repository = await _open_repo()
    try:
        yield repository
    finally:
        await conn.close()


def _stamp(moment: datetime, minutes_ago: float) -> str:
    return (moment - timedelta(minutes=minutes_ago)).strftime(FMT)


async def _store(repo: Repository, pid: int, column: str, value: object) -> None:
    assert column in {
        "check_interval_minutes",
        "last_checked_at",
        "last_error_at",
        "created_at",
        "url",
    }
    await repo._conn.execute(f"UPDATE products SET {column} = ? WHERE id = ?", (value, pid))
    await repo._conn.commit()


async def _add(
    repo: Repository,
    moment: datetime,
    n: int = 1,
    *,
    interval: int | None = None,
    created_minutes_ago: float | None = 1000,
) -> int:
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
    if created_minutes_ago is not None:
        await _store(repo, pid, "created_at", _stamp(moment, created_minutes_ago))
    return pid


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


def _moment() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def _due_run(
    repo: Repository,
    moment: datetime,
    *,
    global_minutes: int = 360,
    scraper: _RecordingScraper | None = None,
    attempted: dict[int, datetime] | None = None,
) -> list[str]:
    scraper = scraper or _RecordingScraper()
    async with httpx.AsyncClient() as client:
        scheduler = _scheduler(repo, scraper, client)
        scheduler._attempted_at.update(attempted or {})
        await scheduler.run_check_due(global_interval_minutes=global_minutes, now=moment)
    return scraper.urls


async def test_own_interval_below_the_floor_is_raised_to_it(repo: Repository) -> None:
    moment = _moment()
    pid = await _add(repo, moment, interval=30)
    await _store(repo, pid, "last_checked_at", _stamp(moment, 40))
    assert await _due_run(repo, moment) == []

    await _store(repo, pid, "last_checked_at", _stamp(moment, 361))
    assert await _due_run(repo, moment) == ["https://example.com/p/1"]


async def test_malformed_own_interval_with_a_short_global_keeps_the_floor(
    repo: Repository,
) -> None:
    moment = _moment()
    pid = await _add(repo, moment)
    await _store(repo, pid, "last_checked_at", _stamp(moment, 359))
    for value in (0, -5, 10**9):
        await _store(repo, pid, "check_interval_minutes", value)
        assert await _due_run(repo, moment, global_minutes=5) == []


async def test_short_global_interval_keeps_the_floor(repo: Repository) -> None:
    moment = _moment()
    pid = await _add(repo, moment)
    await _store(repo, pid, "last_checked_at", _stamp(moment, 100))
    assert await _due_run(repo, moment, global_minutes=5) == []


async def test_never_checked_product_counts_from_its_creation(repo: Repository) -> None:
    moment = _moment()
    await _add(repo, moment, 1, created_minutes_ago=10)
    await _add(repo, moment, 2, created_minutes_ago=361)
    assert await _due_run(repo, moment) == ["https://example.com/p/2"]


async def test_a_recent_failure_holds_the_floor(repo: Repository) -> None:
    moment = _moment()
    pid = await _add(repo, moment)
    await _store(repo, pid, "last_error_at", _stamp(moment, 100))
    await _store(repo, pid, "last_checked_at", _stamp(moment, 400))
    assert await _due_run(repo, moment, global_minutes=5) == []


async def test_a_recent_attempt_in_memory_holds_the_floor(repo: Repository) -> None:
    moment = _moment()
    pid = await _add(repo, moment)
    urls = await _due_run(repo, moment, attempted={pid: moment - timedelta(minutes=100)})
    assert urls == []


async def test_the_floor_has_no_tick_slack(repo: Repository) -> None:
    moment = _moment()
    exact = await _add(repo, moment, 1)
    short = await _add(repo, moment, 2)
    await _store(repo, exact, "last_checked_at", _stamp(moment, 360))
    await _store(repo, short, "last_checked_at", _stamp(moment, 360 - 1 / 60))
    assert await _due_run(repo, moment, global_minutes=5) == ["https://example.com/p/1"]


async def test_a_scraper_without_floor_is_unchanged(repo: Repository) -> None:
    moment = _moment()
    pid = await _add(repo, moment, interval=30)
    await _store(repo, pid, "last_checked_at", _stamp(moment, 40))
    urls = await _due_run(repo, moment, scraper=_UnflooredScraper())
    assert urls == ["https://example.com/p/1"]


async def test_manual_checks_have_no_floor(repo: Repository) -> None:
    moment = _moment()
    pid = await _add(repo, moment)
    await _store(repo, pid, "last_checked_at", _stamp(moment, 10))

    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, scraper, client).run_check_all()
    assert scraper.urls == ["https://example.com/p/1"]

    scraper = _RecordingScraper()
    async with httpx.AsyncClient() as client:
        results = await _scheduler(repo, scraper, client).check_products_for_user(
            product_ids=[pid], user_id=1, delay_between_products=0
        )
    assert scraper.urls == ["https://example.com/p/1"]
    assert [result.reason for result in results] == [None]


_AGO = st.one_of(st.none(), st.integers(0, 2000))


@given(
    own=st.one_of(st.none(), st.integers(-10, 10**5)),
    global_minutes=st.integers(5, 10**4),
    checked=_AGO,
    created=st.integers(0, 2000),
)
@settings(max_examples=40, deadline=None)
def test_a_floored_scraper_is_never_called_inside_its_floor(
    own: int | None, global_minutes: int, checked: int | None, created: int
) -> None:
    async def scenario() -> None:
        conn, repo = await _open_repo()
        try:
            moment = _moment()
            pid = await _add(repo, moment, created_minutes_ago=created)
            if own is not None:
                await _store(repo, pid, "check_interval_minutes", own)
            if checked is not None:
                await _store(repo, pid, "last_checked_at", _stamp(moment, checked))
            urls = await _due_run(repo, moment, global_minutes=global_minutes)
            anchors = [created] if checked is None else [created, checked]
            if urls:
                assert min(anchors) >= FLOOR
        finally:
            await conn.close()

    asyncio.run(scenario())


async def test_unreadable_timestamps_never_lift_the_floor_of_a_valid_anchor(
    repo: Repository,
) -> None:
    moment = _moment()
    bad_values: list[object] = ["", "garbage", "2026-01-0", 5, b"x"]
    for value in bad_values:
        pid = await _add(repo, moment, created_minutes_ago=1000)
        await _store(repo, pid, "last_checked_at", value)
        await _store(repo, pid, "last_error_at", _stamp(moment, 100))
        assert await _due_run(repo, moment, global_minutes=5) == []
        await repo._conn.execute("DELETE FROM products WHERE id = ?", (pid,))
        await repo._conn.commit()


async def test_a_future_creation_holds_the_floor(repo: Repository) -> None:
    moment = _moment()
    pid = await _add(repo, moment)
    await _store(repo, pid, "created_at", (moment + timedelta(days=1)).strftime(FMT))
    assert await _due_run(repo, moment) == []


async def test_the_newest_anchor_wins_whatever_its_kind(repo: Repository) -> None:
    moment = _moment()
    newest_creation = await _add(repo, moment, 1, created_minutes_ago=100)
    await _store(repo, newest_creation, "last_checked_at", _stamp(moment, 400))
    newest_failure = await _add(repo, moment, 2, created_minutes_ago=1000)
    await _store(repo, newest_failure, "last_checked_at", _stamp(moment, 1000))
    await _store(repo, newest_failure, "last_error_at", _stamp(moment, 100))

    # The attempt in memory is the OLDEST anchor here: the stored failure still holds.
    urls = await _due_run(repo, moment, attempted={newest_failure: moment - timedelta(minutes=400)})

    assert urls == []


async def test_with_every_anchor_unreadable_the_product_is_due(
    repo: Repository,
) -> None:
    moment = _moment()
    pid = await _add(repo, moment, created_minutes_ago=None)
    await _store(repo, pid, "created_at", "garbage")
    await _store(repo, pid, "last_checked_at", "not a timestamp")
    await _store(repo, pid, "last_error_at", b"x")
    assert await _due_run(repo, moment) == ["https://example.com/p/1"]


async def test_hostile_urls_do_not_stop_the_sweep(repo: Repository) -> None:
    moment = _moment()
    for n, url in enumerate(["", "not a url", "https://[bad"], start=10):
        pid = await _add(repo, moment, n, created_minutes_ago=None)
        await _store(repo, pid, "url", url)
    await _add(repo, moment, 1, created_minutes_ago=1000)
    held = await _add(repo, moment, 2, created_minutes_ago=10)
    assert held

    assert await _due_run(repo, moment) == ["https://example.com/p/1"]
