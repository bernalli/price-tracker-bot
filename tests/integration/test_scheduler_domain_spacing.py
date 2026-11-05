"""Scheduled checks on the same store are spaced; other stores are not held back.

The spacing is decided per check (job), in the scheduler: a virtual clock drives
it, so no test sleeps for real.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from price_tracker.bot.handlers._helpers import _record_manual_start
from price_tracker.core.domain_spacing import DomainSpacing
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
_GAP = 30.0


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class _TimedScraper(AbstractScraper):
    """Records when each URL is scraped; can fail chosen URLs."""

    name = "timed"
    priority = 100

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.starts: list[tuple[str, float]] = []
        self.failures: dict[str, BaseException] = {}

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        self.starts.append((url.split("/")[2] + url[url.rindex("/") :], self.clock.now))
        failure = self.failures.pop(url, None)
        if failure is not None:
            raise failure
        return ProductInfo(name="Widget", price=Decimal("100"), currency="EUR")


class _Health(HealthManager):
    """Quarantine states fixed per domain; records nothing."""

    def __init__(self, states: dict[str, QuarantineState] | None = None) -> None:
        self.states = states or {}
        self.blocks: list[str] = []

    def state(self, domain: str) -> QuarantineState:
        return self.states.get(domain, QuarantineState.CLOSED)

    def is_locked(self, domain: str) -> bool:
        return self.state(domain).name.startswith("LOCKED")

    def is_half_open(self, domain: str) -> bool:
        return self.state(domain).name.startswith("HALF_OPEN")

    async def record_block(self, domain: str, *, reason: str) -> QuarantineState:
        self.blocks.append(domain)
        return self.state(domain)

    async def record_success(self, domain: str) -> QuarantineState:
        return self.state(domain)


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(user_id=1)
    await repository.ensure_user(user_id=2)
    try:
        yield repository
    finally:
        await conn.close()


async def _add(repo: Repository, url: str, *, user_id: int = 1) -> int:
    return await repo.add_product(
        user_id=user_id,
        url=url,
        name=url,
        domain=url.split("/")[2],
        initial_price=Decimal("100"),
        currency="EUR",
    )


def _scheduler(
    repo: Repository,
    scraper: _TimedScraper,
    clock: _Clock,
    *,
    gap: float = _GAP,
    health: HealthManager | None = None,
) -> Scheduler:
    registry = ScraperRegistry()
    registry.register(scraper)
    return Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=AsyncMock(),
            notifier=AsyncMock(),
            delay_between_products=0,
            health_mgr=health or _Health(),
            domain_spacing=DomainSpacing(
                min_gap=gap,
                clock=clock,
                wall_clock=lambda: datetime(2026, 11, 4, tzinfo=UTC),
                sleep=clock.sleep,
            ),
        )
    )


async def _five_products(repo: Repository) -> None:
    for url in (
        "https://a.com/p/1",
        "https://a.com/p/2",
        "https://a.com/p/3",
        "https://b.com/p/1",
        "https://b.com/p/2",
    ):
        await _add(repo, url)


@pytest.mark.parametrize("sweep", ["due", "all", "user"])
async def test_three_products_on_one_store_two_on_another(repo: Repository, sweep: str) -> None:
    await _five_products(repo)
    clock = _Clock()
    scraper = _TimedScraper(clock)
    scheduler = _scheduler(repo, scraper, clock)

    if sweep == "due":
        await scheduler.run_check_due(global_interval_minutes=360)
    elif sweep == "all":
        await scheduler.run_check_all()
    else:
        await scheduler.run_check_for_user(user_id=1)

    assert scraper.starts == [
        ("a.com/1", 0),
        ("b.com/1", 0),
        ("a.com/2", 30),
        ("b.com/2", 30),
        ("a.com/3", 60),
    ]
    assert clock.sleeps == [30, 30]


async def test_stores_are_interleaved_across_users(repo: Repository) -> None:
    for n in (1, 2, 3):
        await _add(repo, f"https://a.com/p/{n}", user_id=1)
    await _add(repo, "https://b.com/p/1", user_id=2)
    await _add(repo, "https://c.com/p/1", user_id=2)
    clock = _Clock()
    scraper = _TimedScraper(clock)

    await _scheduler(repo, scraper, clock).run_check_due(global_interval_minutes=360)

    assert dict(scraper.starts) == {
        "a.com/1": 0,
        "b.com/1": 0,
        "c.com/1": 0,
        "a.com/2": 30,
        "a.com/3": 60,
    }


async def test_gap_zero_does_not_space(repo: Repository) -> None:
    await _five_products(repo)
    clock = _Clock()
    scraper = _TimedScraper(clock)

    await _scheduler(repo, scraper, clock, gap=0).run_check_due(global_interval_minutes=360)

    assert [at for _url, at in scraper.starts] == [0] * 5
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "failure",
    [
        HTTPBlockStatus(status=429, url="https://a.com/p/1", retry_after="600"),
        httpx.HTTPStatusError(
            "unavailable",
            request=httpx.Request("GET", "https://a.com/p/1"),
            response=httpx.Response(503, headers={"Retry-After": "600"}),
        ),
    ],
    ids=["429-block", "503-status"],
)
async def test_retry_after_defers_the_store_until_it_expires(
    repo: Repository, failure: BaseException
) -> None:
    await _five_products(repo)
    clock = _Clock()
    scraper = _TimedScraper(clock)
    scraper.failures["https://a.com/p/1"] = failure
    health = _Health()
    scheduler = _scheduler(repo, scraper, clock, health=health)

    await scheduler.run_check_due(global_interval_minutes=360)
    assert scraper.starts == [("a.com/1", 0), ("b.com/1", 0), ("b.com/2", 30)]
    if isinstance(failure, HTTPBlockStatus):
        assert health.blocks == ["a.com"]  # the circuit breaker still counts it

    scraper.starts.clear()
    clock.now = 599
    await scheduler.run_check_due(global_interval_minutes=360)
    assert scraper.starts == []  # still deferred: the other products stay due

    clock.now = 600
    await scheduler.run_check_due(global_interval_minutes=360)
    assert scraper.starts == [("a.com/2", 600), ("a.com/3", 630)]


@pytest.mark.parametrize("header", [None, "garbage", "-5"])
async def test_a_block_without_usable_retry_after_does_not_defer(
    repo: Repository, header: str | None
) -> None:
    await _five_products(repo)
    clock = _Clock()
    scraper = _TimedScraper(clock)
    scraper.failures["https://a.com/p/1"] = HTTPBlockStatus(
        status=429, url="https://a.com/p/1", retry_after=header
    )

    await _scheduler(repo, scraper, clock).run_check_due(global_interval_minutes=360)

    assert [url for url, _at in scraper.starts].count("a.com/2") == 1


async def test_a_locked_store_is_skipped_without_waiting(repo: Repository) -> None:
    await _five_products(repo)
    clock = _Clock()
    scraper = _TimedScraper(clock)
    health = _Health({"a.com": QuarantineState.LOCKED_T1})

    await _scheduler(repo, scraper, clock, health=health).run_check_due(global_interval_minutes=360)

    assert scraper.starts == [("b.com/1", 0), ("b.com/2", 30)]


async def test_a_half_open_store_gets_one_probe_and_no_wait(repo: Repository) -> None:
    await _five_products(repo)
    clock = _Clock()
    scraper = _TimedScraper(clock)
    health = _Health({"a.com": QuarantineState.HALF_OPEN_T1})

    await _scheduler(repo, scraper, clock, health=health).run_check_due(global_interval_minutes=360)

    assert scraper.starts == [("a.com/1", 0), ("b.com/1", 0), ("b.com/2", 30)]


async def test_manual_checks_do_not_wait_but_delay_the_next_scheduled_one(
    repo: Repository,
) -> None:
    first = await _add(repo, "https://a.com/p/1")
    second = await _add(repo, "https://a.com/p/2")
    await _add(repo, "https://b.com/p/1")
    clock = _Clock()
    scraper = _TimedScraper(clock)
    scheduler = _scheduler(repo, scraper, clock)

    clock.now = 100
    await scheduler.check_one_product_for_user(product_id=first, user_id=1)
    await scheduler.check_one_product_for_user(product_id=second, user_id=1)
    await scheduler.check_user_products_for_user(user_id=1, delay_between_products=0)
    assert [at for _url, at in scraper.starts] == [100] * 5
    assert clock.sleeps == []

    scraper.starts.clear()
    clock.now = 110
    await scheduler.run_check_all()
    assert scraper.starts == [("a.com/1", 130), ("b.com/1", 130), ("a.com/2", 160)]


async def test_record_manual_start_spaces_the_next_scheduled_check(repo: Repository) -> None:
    await _add(repo, "https://a.com/p/1")
    clock = _Clock()
    scraper = _TimedScraper(clock)
    scheduler = _scheduler(repo, scraper, clock)

    scheduler.record_manual_start("https://www.a.com/some/page")
    scheduler.record_manual_start("not a url")
    clock.now = 5
    await scheduler.run_check_due(global_interval_minutes=360)

    assert scraper.starts == [("a.com/1", 30)]


async def test_a_manual_block_with_retry_after_defers_the_scheduler(repo: Repository) -> None:
    pid = await _add(repo, "https://a.com/p/1")
    await _add(repo, "https://a.com/p/2")
    clock = _Clock()
    scraper = _TimedScraper(clock)
    scraper.failures["https://a.com/p/1"] = HTTPBlockStatus(
        status=429, url="https://a.com/p/1", retry_after="300"
    )
    scheduler = _scheduler(repo, scraper, clock)

    result = await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
    assert result.reason == "block"

    await scheduler.run_check_due(global_interval_minutes=360)
    assert scraper.starts == [("a.com/1", 0)]


async def test_add_and_import_record_their_start_through_the_handler_helper(
    repo: Repository,
) -> None:
    await _add(repo, "https://a.com/p/1")
    clock = _Clock()
    scraper = _TimedScraper(clock)
    scheduler = _scheduler(repo, scraper, clock)

    _record_manual_start(SimpleNamespace(bot_data={"scheduler": scheduler}), "https://a.com/new")
    _record_manual_start(SimpleNamespace(bot_data={}), "https://a.com/new")
    _record_manual_start(SimpleNamespace(), "https://a.com/new")
    await scheduler.run_check_due(global_interval_minutes=360)

    assert scraper.starts == [("a.com/1", 30)]
