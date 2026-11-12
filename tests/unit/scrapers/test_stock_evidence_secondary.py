"""Stock evidence remains bound to the selected secondary-site offer."""

from decimal import Decimal

import pytest

from tests.unit.scrapers.test_remaining_evidence_regressions import STORES, product, scrape

OWN = [
    s
    for s in STORES
    if s[0] in {"zalando", "apple_store", "etsy", "google_store", "wayfair", "mediamarkt", "otto"}
]


@pytest.mark.parametrize("store", OWN, ids=lambda s: s[0])
@pytest.mark.parametrize(
    "stock", [None, "https://schema.org/InStock", "https://schema.org/OutOfStock"]
)
async def test_offer_stock_evidence(store, stock):
    offer = {"price": "100.00", "priceCurrency": "EUR"}
    if stock is not None:
        offer["availability"] = stock
    info = await scrape(store, product(offer))
    assert info.stock_evidence is (stock is not None)
    if stock is not None:
        assert info.available is stock.endswith("/InStock")


@pytest.mark.parametrize("stock", [None, True, False])
async def test_shopify_json_variant_requires_explicit_selected_stock(monkeypatch, stock):
    from unittest.mock import AsyncMock

    import httpx

    from price_tracker.scrapers import shopify

    url = "https://shop.example.com/products/item"
    variant = {"id": 1, "price": "20.00", "price_currency": "EUR"}
    if stock is not None:
        variant["available"] = stock
    response = httpx.Response(
        200,
        json={"product": {"handle": "item", "variants": [variant]}},
        request=httpx.Request("GET", url + ".json"),
    )
    monkeypatch.setattr(shopify, "_fetch_shopify_json", AsyncMock(return_value=response))
    async with httpx.AsyncClient() as client:
        info = await shopify.ShopifyScraper()._try_json_api(
            url + ".json", client, requested_url=url
        )
    assert info is not None
    assert info.stock_evidence is (stock is not None)
    if stock is not None:
        assert info.available is stock


@pytest.mark.parametrize(
    "stock", [None, "https://schema.org/InStock", "https://schema.org/OutOfStock"]
)
async def test_amazon_json_selected_offer_stock_evidence(monkeypatch, stock):
    from unittest.mock import AsyncMock

    import httpx

    from price_tracker.core.page_image import ImagePageHTML
    from price_tracker.scrapers import amazon

    url = "https://www.amazon.com/dp/B012345678"
    offer = {"price": "20.00", "priceCurrency": "USD"}
    if stock is not None:
        offer["availability"] = stock
    import json

    html = ImagePageHTML(
        '<script type="application/ld+json">'
        + json.dumps({"@type": "Product", "url": url, "name": "Item", "offers": offer})
        + "</script>",
        url,
    )
    monkeypatch.setattr(amazon, "_fetch_amazon_page", AsyncMock(return_value=html))
    async with httpx.AsyncClient() as client:
        info = await amazon.AmazonScraper().scrape(url, client)
    assert info.price is not None
    assert info.stock_evidence is (stock is not None)
    if stock is not None:
        assert info.available is stock.endswith("/InStock")


@pytest.mark.parametrize(
    "stock", [None, "https://schema.org/InStock", "https://schema.org/OutOfStock"]
)
async def test_aliexpress_selected_offer_stock(monkeypatch, stock):
    from unittest.mock import AsyncMock

    import httpx

    from price_tracker.scrapers import aliexpress

    url = "https://www.aliexpress.com/item/1.html"
    offer = {"price": "100.00", "priceCurrency": "EUR"}
    if stock is not None:
        offer["availability"] = stock
    response = httpx.Response(200, text=product(offer), request=httpx.Request("GET", url))
    monkeypatch.setattr(aliexpress, "_fetch_aliexpress_html", AsyncMock(return_value=response))
    async with httpx.AsyncClient() as client:
        info = await aliexpress.AliexpressScraper().scrape(url, client)
    assert info.stock_evidence is (stock is not None)
    if stock is not None:
        assert info.available is stock.endswith("/InStock")


async def test_amazon_see_other_sellers_is_not_sold_out_evidence(monkeypatch):
    from unittest.mock import AsyncMock

    import httpx

    from price_tracker.core.page_image import ImagePageHTML
    from price_tracker.scrapers import amazon

    url = "https://www.amazon.com/dp/B012345678"
    html = ImagePageHTML('<div id="buybox-see-all-buying-choices">Other sellers</div>', url)
    monkeypatch.setattr(amazon, "_fetch_amazon_page", AsyncMock(return_value=html))
    async with httpx.AsyncClient() as client:
        info = await amazon.AmazonScraper().scrape(url, client)
    assert info.stock_evidence is False
    assert info.price is None


async def test_generic_does_not_borrow_unpriced_stock_from_other_strategy(monkeypatch):
    from unittest.mock import AsyncMock

    import httpx

    from price_tracker.scrapers import generic

    monkeypatch.setattr(generic, "_fetch_generic_html", AsyncMock(return_value="<html></html>"))
    monkeypatch.setattr(
        generic.GenericScraper,
        "_try_json_ld_bound",
        lambda *args: {"price": Decimal("20"), "currency": "EUR"},
    )
    monkeypatch.setattr(
        generic.GenericScraper, "_try_microdata", lambda *args, **kwargs: {"available": False}
    )
    async with httpx.AsyncClient() as client:
        info = await generic.GenericScraper().scrape("https://shop.example/item", client)
    assert info.price == Decimal("20")
    assert info.stock_evidence is False


def test_shopify_matching_groups_with_conflicting_stock_remain_unknown():
    import json

    from price_tracker.scrapers.shopify import ShopifyScraper

    url = "https://shop.example.com/products/widget"
    groups = [
        {
            "@type": "ProductGroup",
            "url": url,
            "name": "Widget",
            "hasVariant": [
                {
                    "@type": "Product",
                    "sku": "W",
                    "offers": {"price": "20.00", "priceCurrency": "USD", "availability": stock},
                }
            ],
        }
        for stock in ("https://schema.org/InStock", "https://schema.org/OutOfStock")
    ]
    html = '<script type="application/ld+json">' + json.dumps(groups) + "</script>"
    info = ShopifyScraper._try_product_group(html, url)
    assert info is not None
    assert info.price == Decimal("20.00")
    assert info.stock_evidence is False
