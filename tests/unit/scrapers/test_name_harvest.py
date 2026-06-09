"""Later strategies must still contribute name/currency after price is found (#48).

When the first strategy returns a price but no name, the scrapers skipped every
later strategy entirely, dropping the real product name and falling back to the
generic <title>.
"""

from __future__ import annotations

import httpx
import respx

from price_tracker.scrapers.target import TargetScraper

_HTML = (
    "<html><head><title>Generic Page Title — Target</title>"
    '<script type="application/ld+json">'
    '{"@type":"Product","name":"Real Product Name",'
    '"offers":{"@type":"Offer","price":"50.00","priceCurrency":"USD"}}'
    "</script></head><body>"
    '<span data-test="product-price">$49.99</span>'  # DOM price, no product-title
    "</body></html>"
)


async def test_target_harvests_name_from_later_strategy() -> None:
    url = "https://www.target.com/p/widget"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=_HTML)
        async with httpx.AsyncClient() as client:
            info = await TargetScraper().scrape(url, client)
    assert info.price is not None
    assert info.name == "Real Product Name"  # not the generic <title>
