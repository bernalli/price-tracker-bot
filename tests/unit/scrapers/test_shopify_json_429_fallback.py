"""A 429 on the Shopify ``.json`` endpoint falls back to the product page.

Some stores rate-limit the ``.json`` API per address and path while the HTML
product page of the same product still answers 200. Raising the block straight
away quarantined those stores for days although the price was one request away.
The block is reported only when the page is refused too.
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
import respx

from price_tracker.core.exceptions import BlockEvent, HTTPBlockStatus, ListingGone, WAFBlocked
from price_tracker.core.health import HealthManager, QuarantineState
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.scrapers.shopify import ShopifyScraper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from price_tracker.core.scraper_base import ProductInfo

_URL = "https://shop.example.com/products/haywood-white-wood-ash"
_JSON_URL = f"{_URL}.json"
# The theme's embedded product JSON, as a real Shopify product page renders it:
# prices in integer cents.
_PAGE = (
    '<html><head><script>var meta = {"product":{"id":1,"vendor":"Shop",'
    '"handle":"haywood-white-wood-ash","variants":['
    '{"id":11,"price":19900,"price_currency":"EUR","name":"HAYWOOD - 36","sku":"A1"}]},'
    '"page":{"pageType":"product"}};</script></head><body></body></html>'
)
_MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


async def _scrape(page_status: int, page_text: str = "") -> tuple[ProductInfo, respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        router.get(_JSON_URL).respond(429, text="Too Many Requests", headers={"Retry-After": "0"})
        page_route = router.get(_URL).respond(page_status, text=page_text)
        async with httpx.AsyncClient() as client:
            info = await ShopifyScraper().scrape(_URL, client)
    return info, page_route


async def test_json_429_reads_the_price_from_the_product_page() -> None:
    info, page_route = await _scrape(200, _PAGE)

    assert page_route.call_count == 1
    assert info.price == Decimal("199.00")
    assert info.error is None


async def test_json_429_and_page_429_raise_the_block() -> None:
    with pytest.raises(HTTPBlockStatus) as exc_info:
        await _scrape(429, "Too Many Requests")

    assert exc_info.value.status == 429
    assert exc_info.value.url == _URL  # the request that was refused last


async def test_json_429_and_page_403_raise_the_block() -> None:
    with pytest.raises(HTTPBlockStatus) as exc_info:
        await _scrape(403, "Forbidden")

    assert exc_info.value.status == 403


async def test_json_429_and_challenge_page_raise_the_block() -> None:
    with pytest.raises(WAFBlocked):
        await _scrape(200, "<html><title>Just a moment...</title></html>")


async def test_json_429_and_captcha_page_raise_the_block() -> None:
    with pytest.raises(BlockEvent):
        await _scrape(200, '<html><body><form id="captcha-form">verify</form></body></html>')


async def test_json_429_and_page_unreachable_raise_the_json_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing contradicts the JSON block when the page cannot be read at all."""

    async def _page_down(url: str, client: httpx.AsyncClient) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    from price_tracker.scrapers import shopify as shopify_module

    monkeypatch.setattr(shopify_module, "_fetch_shopify_response", _page_down)
    with pytest.raises(HTTPBlockStatus) as exc_info:
        await _scrape(200, _PAGE)

    assert exc_info.value.status == 429
    assert exc_info.value.url == _JSON_URL


async def test_json_429_and_page_gone_is_a_gone_listing() -> None:
    with pytest.raises(ListingGone):
        await _scrape(404)


async def test_json_403_is_still_a_block_without_reading_the_page() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_JSON_URL).respond(403, text="Forbidden")
        page_route = router.get(_URL).respond(200, text=_PAGE)
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus) as exc_info:
                await ShopifyScraper().scrape(_URL, client)

    assert exc_info.value.status == 403
    assert page_route.call_count == 0


# ── through the scheduler: the domain is not quarantined ──


@pytest_asyncio.fixture
async def repo_with_shopify_product() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, _MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    await repo.add_product(
        user_id=1,
        url=_URL,
        name="HAYWOOD",
        domain="example.com",
        initial_price=Decimal("199.00"),
        currency="EUR",
    )
    try:
        yield repo
    finally:
        await conn.close()


async def _run_sweeps(repo: Repository, page_status: int, page_text: str, sweeps: int) -> str:
    registry = ScraperRegistry()
    registry.register(ShopifyScraper())
    health_mgr = HealthManager(repo)
    await health_mgr.load()
    with respx.mock(assert_all_called=False) as router:
        router.get(_JSON_URL).respond(429, text="Too Many Requests")
        router.get(_URL).respond(page_status, text=page_text)
        async with httpx.AsyncClient() as client:
            scheduler = Scheduler(
                SchedulerDeps(
                    repo=repo,
                    registry=registry,
                    client=client,
                    notifier=AsyncMock(),
                    max_consecutive_errors=99,
                    delay_between_products=0.0,
                    health_mgr=health_mgr,
                )
            )
            for _ in range(sweeps):
                await scheduler.run_check_for_user(user_id=1)
    return health_mgr.state("example.com").value


async def test_json_429_with_readable_page_keeps_the_domain_closed(
    repo_with_shopify_product: Repository,
) -> None:
    state = await _run_sweeps(repo_with_shopify_product, 200, _PAGE, sweeps=3)

    assert state == QuarantineState.CLOSED.value
    row = await repo_with_shopify_product.get_scraper_health("example.com")
    assert row is not None
    assert row.consecutive_blocks == 0
    assert row.last_success_at is not None


async def test_json_429_with_refused_page_still_quarantines(
    repo_with_shopify_product: Repository,
) -> None:
    state = await _run_sweeps(repo_with_shopify_product, 429, "Too Many Requests", sweeps=3)

    assert state == QuarantineState.LOCKED_T1.value
