"""JSON-LD offer selection must skip financing/cheapest-variant entries (#9).

Apple/Google list a monthly-financing offer (and sometimes a cheaper variant)
in offers[]; the scrapers took offers[0], leaking e.g. "$54.08/mo" as THE price.
"""

from __future__ import annotations

from decimal import Decimal

from bs4 import BeautifulSoup

from price_tracker.scrapers.apple_store import AppleStoreScraper
from price_tracker.scrapers.google_store import GoogleStoreScraper

_HTML = (
    '<html><head><script type="application/ld+json">'
    '{"@type":"Product","name":"Phone","offers":['
    '{"@type":"Offer","price":"54.08","priceCurrency":"USD","name":"$54.08/mo. for 24 months"},'
    '{"@type":"Offer","price":"1299.00","priceCurrency":"USD"}]}'
    "</script></head><body></body></html>"
)


def test_apple_jsonld_skips_financing_offer() -> None:
    result = AppleStoreScraper()._try_jsonld(BeautifulSoup(_HTML, "lxml"))
    assert result is not None
    assert result["price"] == Decimal("1299.00")


def test_google_jsonld_skips_financing_offer() -> None:
    result = GoogleStoreScraper()._try_jsonld(BeautifulSoup(_HTML, "lxml"))
    assert result is not None
    assert result["price"] == Decimal("1299.00")


import pytest  # noqa: E402

from price_tracker.scrapers.bestbuy import BestbuyScraper  # noqa: E402
from price_tracker.scrapers.etsy import EtsyScraper  # noqa: E402
from price_tracker.scrapers.mediamarkt import MediamarktScraper  # noqa: E402
from price_tracker.scrapers.newegg import NeweggScraper  # noqa: E402
from price_tracker.scrapers.otto import OttoScraper  # noqa: E402
from price_tracker.scrapers.target import TargetScraper  # noqa: E402
from price_tracker.scrapers.walmart import WalmartScraper  # noqa: E402
from price_tracker.scrapers.wayfair import WayfairScraper  # noqa: E402
from price_tracker.scrapers.zalando import ZalandoScraper  # noqa: E402


@pytest.mark.parametrize(
    "scraper_cls",
    [
        TargetScraper,
        NeweggScraper,
        EtsyScraper,
        OttoScraper,
        BestbuyScraper,
        WayfairScraper,
        WalmartScraper,
        MediamarktScraper,
        ZalandoScraper,
    ],
    ids=lambda c: c.__name__,
)
def test_scraper_jsonld_skips_financing_offer(scraper_cls) -> None:
    result = scraper_cls()._try_jsonld(BeautifulSoup(_HTML, "lxml"))
    assert result is not None
    assert result["price"] == Decimal("1299.00")
