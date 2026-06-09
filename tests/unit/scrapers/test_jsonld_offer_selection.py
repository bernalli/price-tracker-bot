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
