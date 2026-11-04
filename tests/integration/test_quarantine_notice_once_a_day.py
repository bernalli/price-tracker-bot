"""One quarantine notice per store per user per day, and it survives a restart.

A store that keeps rate-limiting re-enters quarantine every few hours: the
circuit breaker closes after a successful probe and the next blocks lock it
again. Each entry pushed its own "Site auto-paused" message (four in one day).
The notice is now pushed at most once per 24 hours for the same user and store;
the /errors view keeps showing the quarantine all along.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest_asyncio
from freezegun import freeze_time

from price_tracker.bot.handlers.debug import errors_text
from price_tracker.core.exceptions import HTTPBlockStatus
from price_tracker.core.health import HealthManager, QuarantineState
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

_MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
_DOMAIN = "store.com"
_START = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


class _SwitchScraper(AbstractScraper):
    """Blocks while ``blocking`` is set, reads a price otherwise."""

    name = "switch-stub"
    priority = 100

    def __init__(self) -> None:
        self.blocking = True

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        if self.blocking:
            raise HTTPBlockStatus(status=429, url=url)
        return ProductInfo(name="Widget", price=Decimal("90"), currency="EUR")


class _Notifier:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def __call__(
        self,
        user_id: int,
        text: str,
        *,
        product_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        self.payloads.append({"user_id": user_id, **(payload or {})})
        return True

    def quarantine_notices(self, user_id: int) -> int:
        return sum(
            1
            for p in self.payloads
            if p.get("event") == "quarantine" and p.get("user_id") == user_id
        )


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, _MIGRATIONS_DIR)
    repository = Repository(conn)
    for user_id in (1, 2):
        await repository.ensure_user(user_id=user_id)
        await repository.add_product(
            user_id=user_id,
            url=f"https://{_DOMAIN}/products/widget",
            name="Widget",
            domain=_DOMAIN,
            initial_price=Decimal("100"),
            currency="EUR",
        )
    try:
        yield repository
    finally:
        await conn.close()


async def _boot(repo: Repository, scraper: _SwitchScraper, notifier: _Notifier) -> Scheduler:
    """A fresh scheduler and health manager on the same database: a restart."""
    registry = ScraperRegistry()
    registry.register(scraper)
    health_mgr = HealthManager(repo)
    await health_mgr.load()
    return Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=AsyncMock(),  # the stub scraper never uses it
            notifier=notifier,
            max_consecutive_errors=99,
            delay_between_products=0.0,
            health_mgr=health_mgr,
        )
    )


async def _enter_quarantine(scheduler: Scheduler, scraper: _SwitchScraper) -> None:
    scraper.blocking = True
    for _ in range(3):
        await scheduler.run_check_for_user(user_id=1)
    assert scheduler.deps.health_mgr.state(_DOMAIN) == QuarantineState.LOCKED_T1


async def _recover(scheduler: Scheduler, scraper: _SwitchScraper, frozen: Any) -> None:
    frozen.tick(timedelta(hours=1, minutes=1))  # lock expires → half-open probe
    scraper.blocking = False
    await scheduler.run_check_for_user(user_id=1)
    assert scheduler.deps.health_mgr.state(_DOMAIN) == QuarantineState.CLOSED


async def test_reentering_quarantine_within_a_day_does_not_push_again(repo: Repository) -> None:
    scraper, notifier = _SwitchScraper(), _Notifier()
    with freeze_time(_START) as frozen:
        scheduler = await _boot(repo, scraper, notifier)
        await _enter_quarantine(scheduler, scraper)
        assert notifier.quarantine_notices(1) == 1

        await _recover(scheduler, scraper, frozen)
        await _enter_quarantine(scheduler, scraper)

        assert notifier.quarantine_notices(1) == 1
        # The quarantine itself stays visible where the user looks for it.
        text = await errors_text(repo, scheduler.deps.health_mgr, 1)
        assert text is not None
        assert "🔒" in text


async def test_the_daily_memory_survives_a_restart(repo: Repository) -> None:
    scraper, notifier = _SwitchScraper(), _Notifier()
    with freeze_time(_START) as frozen:
        scheduler = await _boot(repo, scraper, notifier)
        await _enter_quarantine(scheduler, scraper)
        await _recover(scheduler, scraper, frozen)

        restarted = await _boot(repo, scraper, notifier)
        await _enter_quarantine(restarted, scraper)

    assert notifier.quarantine_notices(1) == 1


async def test_a_quarantine_a_day_later_is_pushed_again(repo: Repository) -> None:
    scraper, notifier = _SwitchScraper(), _Notifier()
    with freeze_time(_START) as frozen:
        scheduler = await _boot(repo, scraper, notifier)
        await _enter_quarantine(scheduler, scraper)
        await _recover(scheduler, scraper, frozen)

        frozen.move_to(_START + timedelta(hours=24, minutes=1))
        await _enter_quarantine(scheduler, scraper)

    assert notifier.quarantine_notices(1) == 2


async def test_the_memory_is_per_user(repo: Repository) -> None:
    scraper, notifier = _SwitchScraper(), _Notifier()
    with freeze_time(_START):
        scheduler = await _boot(repo, scraper, notifier)
        await _enter_quarantine(scheduler, scraper)
        product = await repo.list_products_for_user(user_id=2, only_active=True)
        await scheduler._notify_quarantine_entry(product[0], _DOMAIN, reason="HTTP 429")

    assert notifier.quarantine_notices(1) == 1
    assert notifier.quarantine_notices(2) == 1


async def test_an_undelivered_notice_is_not_remembered(repo: Repository) -> None:
    scraper = _SwitchScraper()
    failing = _Notifier()

    async def _fail(*args: object, **kwargs: object) -> bool:
        return False

    with freeze_time(_START):
        scheduler = await _boot(repo, scraper, failing)
        scheduler.deps.notifier = _fail
        product = (await repo.list_products_for_user(user_id=1, only_active=True))[0]
        await scheduler._notify_quarantine_entry(product, _DOMAIN, reason="HTTP 429")

        working = _Notifier()
        scheduler.deps.notifier = working
        await scheduler._notify_quarantine_entry(product, _DOMAIN, reason="HTTP 429")

    assert working.quarantine_notices(1) == 1


async def test_a_memory_from_the_future_does_not_silence_notices(repo: Repository) -> None:
    """A clock that jumped back must not mute the notice until it catches up."""
    scraper, notifier = _SwitchScraper(), _Notifier()
    with freeze_time(_START + timedelta(days=30)):
        scheduler = await _boot(repo, scraper, notifier)
        product = (await repo.list_products_for_user(user_id=1, only_active=True))[0]
        await scheduler._notify_quarantine_entry(product, _DOMAIN, reason="HTTP 429")
    with freeze_time(_START):
        await scheduler._notify_quarantine_entry(product, _DOMAIN, reason="HTTP 429")

    assert notifier.quarantine_notices(1) == 2


async def test_an_unreadable_memory_does_not_silence_notices(repo: Repository) -> None:
    scraper, notifier = _SwitchScraper(), _Notifier()
    with freeze_time(_START):
        scheduler = await _boot(repo, scraper, notifier)
        product = (await repo.list_products_for_user(user_id=1, only_active=True))[0]
        await scheduler._notify_quarantine_entry(product, _DOMAIN, reason="HTTP 429")
        for config_key in await _notice_keys(repo):
            await repo.set_config(config_key, "not a timestamp")
        await scheduler._notify_quarantine_entry(product, _DOMAIN, reason="HTTP 429")

    assert notifier.quarantine_notices(1) == 2


async def _notice_keys(repo: Repository) -> list[str]:
    cursor = await repo._conn.execute(  # noqa: SLF001 - inspect the stored memory directly
        "SELECT key FROM bot_config WHERE key LIKE 'quarantine_notice:%'"
    )
    rows = await cursor.fetchall()
    keys = [row["key"] for row in rows]
    assert keys, "the notice memory was not persisted"
    return keys
