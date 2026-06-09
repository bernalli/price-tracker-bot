"""Split-span DOM prices must fall back to JSON-LD, not report 1000x too low (bug #12).

React renders "$1,299" and "99" as adjacent spans; get_text concatenates them to
"$1,29999", which parse_price now rejects so the scraper falls back to the
reliable JSON-LD price instead of recording $1.30.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import respx

from price_tracker.scrapers.bestbuy import BestbuyScraper
from price_tracker.scrapers.target import TargetScraper

_JSONLD = (
    '<script type="application/ld+json">'
    '{"@type":"Product","name":"Widget","offers":'
    '{"@type":"Offer","price":"1299.99","priceCurrency":"USD"}}'
    "</script>"
)


def _target_html() -> str:
    return (
        "<html><head>" + _JSONLD + "</head><body>"
        '<span data-test="product-price"><span>$1,299</span><span>99</span></span>'
        "</body></html>"
    )


def _bestbuy_html() -> str:
    return (
        "<html><head>" + _JSONLD + "</head><body>"
        '<div class="priceView-customer-price"><span><span>$1,299</span>'
        "<span>99</span></span></div></body></html>"
    )


async def test_target_split_span_falls_back_to_jsonld() -> None:
    url = "https://www.target.com/p/widget"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=_target_html())
        async with httpx.AsyncClient() as client:
            info = await TargetScraper().scrape(url, client)
    assert info.price == Decimal("1299.99")


async def test_bestbuy_split_span_falls_back_to_jsonld() -> None:
    url = "https://www.bestbuy.com/site/widget/123.p"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=_bestbuy_html())
        async with httpx.AsyncClient() as client:
            info = await BestbuyScraper().scrape(url, client)
    assert info.price == Decimal("1299.99")
