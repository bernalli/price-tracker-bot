"""Through the scheduler, a Shopify refusal is one request, one block, no page fallback."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio
import respx

from price_tracker.core.domain_spacing import MAX_DEFERRAL_SECONDS, DomainSpacing
from price_tracker.core.health import HealthManager, QuarantineState
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.scrapers.shopify import ShopifyScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_URL = "https://shop.example.com/products/haywood-white-wood-ash"
_JSON_URL = f"{_URL}.json"
_PAGE = (
    '<html><head><script>var meta = {"product":{"id":1,"vendor":"Shop",'
    '"handle":"haywood-white-wood-ash","variants":['
    '{"id":11,"price":19900,"price_currency":"EUR","name":"HAYWOOD - 36","sku":"A1"}]},'
    '"page":{"pageType":"product"}};</script></head><body></body></html>'
)
_MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
_DOMAIN = "example.com"


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, _MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(user_id=1)
    pid = await repository.add_product(
        user_id=1,
        url=_URL,
        name="HAYWOOD",
        domain=_DOMAIN,
        initial_price=Decimal("199.00"),
        currency="EUR",
    )
    await conn.execute(
        "UPDATE products SET created_at = datetime('now', '-1000 minutes') WHERE id = ?",
        (pid,),
    )
    await conn.commit()
    try:
        yield repository
    finally:
        await conn.close()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _scheduler(
    repo: Repository, client: httpx.AsyncClient, spacing: DomainSpacing | None = None
) -> tuple[Scheduler, HealthManager]:
    registry = ScraperRegistry()
    registry.register(ShopifyScraper())
    health = HealthManager(repo)
    await health.load()
    scheduler = Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=client,
            notifier=AsyncMock(),
            max_consecutive_errors=99,
            delay_between_products=0.0,
            health_mgr=health,
            domain_spacing=spacing if spacing is not None else DomainSpacing(min_gap=0),
        )
    )
    return scheduler, health


async def _blocks(repo: Repository) -> int:
    row = await repo.get_scraper_health(_DOMAIN)
    return 0 if row is None else row.consecutive_blocks


@pytest.mark.parametrize(
    ("header", "deferral"),
    [(None, 0.0), ("abc", 0.0), ("60", 60.0), ("9" * 400, MAX_DEFERRAL_SECONDS)],
    ids=["none", "text", "sixty", "huge"],
)
async def test_a_json_429_defers_the_store_as_its_retry_after_asks(
    repo: Repository, header: str | None, deferral: float
) -> None:
    clock = _Clock()
    spacing = DomainSpacing(
        min_gap=0, clock=clock, wall_clock=lambda: datetime(2026, 11, 13, tzinfo=UTC)
    )
    headers = {"Retry-After": header} if header is not None else {}
    with respx.mock(assert_all_called=False) as router:
        json_route = router.get(_JSON_URL).respond(429, headers=headers, text="no")
        page_route = router.get(_URL).respond(200, text=_PAGE)
        async with httpx.AsyncClient() as client:
            scheduler, _health = await _scheduler(repo, client, spacing)
            await scheduler.run_check_for_user(user_id=1)

    assert json_route.call_count == 1
    assert page_route.call_count == 0
    assert await _blocks(repo) == 1
    assert spacing.deferred_for(_DOMAIN) == deferral
    if deferral:
        clock.now += deferral / 2
        assert spacing.deferred_for(_DOMAIN) == deferral / 2
        spacing.defer(_DOMAIN, "1")
        assert spacing.deferred_for(_DOMAIN) == deferral / 2
        clock.now += deferral / 2
        assert not spacing.is_deferred(_DOMAIN)


async def test_three_json_429_quarantine_the_store_without_reading_the_page(
    repo: Repository,
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_JSON_URL).respond(429, text="no")
        page_route = router.get(_URL).respond(200, text=_PAGE)
        async with httpx.AsyncClient() as client:
            scheduler, health = await _scheduler(repo, client)
            for _ in range(3):
                await scheduler.run_check_for_user(user_id=1)

    assert health.state(_DOMAIN) == QuarantineState.LOCKED_T1
    assert page_route.call_count == 0


async def test_a_manual_check_stops_at_the_json_refusal(repo: Repository) -> None:
    products = await repo.list_products_for_user(user_id=1)
    with respx.mock(assert_all_called=False) as router:
        json_route = router.get(_JSON_URL).respond(429, text="no")
        page_route = router.get(_URL).respond(200, text=_PAGE)
        async with httpx.AsyncClient() as client:
            scheduler, _health = await _scheduler(repo, client)
            result = await scheduler.check_one_product_for_user(
                product_id=products[0].id, user_id=1
            )

    assert result.reason == "block"
    assert json_route.call_count == 1
    assert page_route.call_count == 0
    assert await _blocks(repo) == 1
