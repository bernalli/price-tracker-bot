"""Every HTML price fallback retains the page's declared currency."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from price_tracker.scrapers.shopify import ShopifyScraper

_URL = "https://store.com/products/widget"


@pytest.mark.parametrize("status", [404, 500])
@pytest.mark.parametrize(
    "price_html",
    [
        '<script>var meta = {"product":{"handle":"widget",'
        '"variants":[{"id":1,"price":19900}]}};</script>',
        '<script>{"price_min":19900}</script>',
        '<span class="product__price">199.00</span>',
    ],
    ids=["embedded", "cents", "css"],
)
@pytest.mark.parametrize(
    "currency_html",
    [
        '<meta property="og:price:currency" content="usd">',
        '<meta property="product:price:currency" content="USD">',
        '<script type="application/ld+json">{"@type":"Product",'
        '"offers":{"priceCurrency":"USD"}}</script>',
        '<script>Shopify.currency = {"active":"USD"};</script>',
    ],
    ids=["og", "product-meta", "jsonld", "shopify-js"],
)
async def test_page_currency_survives_every_fallback(
    status: int, price_html: str, currency_html: str
) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{_URL}.json").respond(status)
        router.get(_URL).respond(200, text=currency_html + price_html)
        async with httpx.AsyncClient() as client:
            info = await ShopifyScraper().scrape(_URL, client)
    if '"price_min"' in price_html:
        # An unbound page-wide amount may be a recommendation, not this product.
        assert info.price is None
    else:
        assert info.price == Decimal("199.00")
        assert info.currency == "USD"
