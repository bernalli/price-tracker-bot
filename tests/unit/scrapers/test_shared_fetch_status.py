"""Every scraper reports a removed listing and a block the same way.

Eleven scrapers fetched with `public_request`, ran `detect_block_event`, then
`raise_for_status`: a 404 or 410 product page became a generic "HTTP error"
read failure, so the removed-listing detection never suspended those products.
eBay had the opposite gap: a 403 or 429 was never reported as a block, so the
domain was never quarantined. They now fetch through `core.fetch.fetch_page`,
which raises `ListingGone` and `BlockEvent` before any status handling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.core.exceptions import BlockEvent, ListingGone
from price_tracker.scrapers.apple_store import AppleStoreScraper
from price_tracker.scrapers.bestbuy import BestbuyScraper
from price_tracker.scrapers.ebay import EbayScraper
from price_tracker.scrapers.etsy import EtsyScraper
from price_tracker.scrapers.google_store import GoogleStoreScraper
from price_tracker.scrapers.mediamarkt import MediamarktScraper
from price_tracker.scrapers.newegg import NeweggScraper
from price_tracker.scrapers.otto import OttoScraper
from price_tracker.scrapers.target import TargetScraper
from price_tracker.scrapers.walmart import WalmartScraper
from price_tracker.scrapers.wayfair import WayfairScraper
from price_tracker.scrapers.zalando import ZalandoScraper

if TYPE_CHECKING:
    from price_tracker.core.scraper_base import AbstractScraper

URL = "https://shop.example.com/p/1"

SCRAPERS: list[type[AbstractScraper]] = [
    AppleStoreScraper,
    BestbuyScraper,
    EbayScraper,
    EtsyScraper,
    GoogleStoreScraper,
    MediamarktScraper,
    NeweggScraper,
    OttoScraper,
    TargetScraper,
    WalmartScraper,
    WayfairScraper,
    ZalandoScraper,
]


async def _scrape(scraper_type: type[AbstractScraper], status: int) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).respond(status, text="<html><body>gone or blocked</body></html>")
        async with httpx.AsyncClient() as client:
            await scraper_type().scrape(URL, client)


@pytest.mark.parametrize("scraper_type", SCRAPERS, ids=lambda t: t.__name__)
@pytest.mark.parametrize("status", [404, 410])
async def test_a_removed_listing_raises_listing_gone(
    scraper_type: type[AbstractScraper], status: int
) -> None:
    with pytest.raises(ListingGone):
        await _scrape(scraper_type, status)


@pytest.mark.parametrize("scraper_type", SCRAPERS, ids=lambda t: t.__name__)
@pytest.mark.parametrize("status", [403, 429])
async def test_a_blocked_page_raises_a_block_event(
    scraper_type: type[AbstractScraper], status: int
) -> None:
    with pytest.raises(BlockEvent):
        await _scrape(scraper_type, status)
