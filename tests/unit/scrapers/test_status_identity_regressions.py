from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import pytest

from price_tracker.core.exceptions import CaptchaDetected
from price_tracker.core.fetch import _fetch_primary
from price_tracker.core.page_image import ImagePageHTML
from price_tracker.scrapers import amazon, generic, shopify
from price_tracker.scrapers.amazon import AmazonScraper
from price_tracker.scrapers.generic import GenericScraper
from price_tracker.scrapers.shopify import ShopifyScraper

if TYPE_CHECKING:
    from price_tracker.core.scraper_base import ProductInfo

pytestmark = pytest.mark.asyncio


async def _shopify_api(monkeypatch, url, variants) -> ProductInfo:
    body = {"product": {"handle": "item", "title": "Item", "variants": variants}}
    json_url = ShopifyScraper()._build_json_url(url)
    assert json_url is not None
    response = httpx.Response(200, json=body, request=httpx.Request("GET", json_url))
    monkeypatch.setattr(shopify, "_fetch_shopify_json", AsyncMock(return_value=response))
    monkeypatch.setattr(ShopifyScraper, "_fetch_html", AsyncMock(return_value=None))
    async with httpx.AsyncClient() as client:
        return await ShopifyScraper().scrape(url, client)


async def test_shopify_selected_variant_is_not_replaced_with_available_variant(monkeypatch):
    result = await _shopify_api(
        monkeypatch,
        "https://shop.example.com/products/item?variant=2",
        [
            {"id": 1, "price": "10.00", "price_currency": "EUR", "available": True},
            {"id": 2, "price": "20.00", "price_currency": "EUR", "available": False},
        ],
    )
    assert result.available is False
    assert result.price == Decimal("20.00")


@pytest.mark.parametrize("selector", ["3", "2&variant=1", "bad"])
async def test_shopify_unknown_or_ambiguous_variant_never_uses_another(monkeypatch, selector):
    result = await _shopify_api(
        monkeypatch,
        f"https://shop.example.com/products/item?variant={selector}",
        [
            {"id": 1, "price": "10.00", "price_currency": "EUR", "available": True},
            {"id": 2, "price": "20.00", "price_currency": "EUR", "available": False},
        ],
    )
    assert result.price is None
    assert result.available is not False


async def test_shopify_cannot_infer_sold_out_from_priced_unavailable_variant(monkeypatch):
    result = await _shopify_api(
        monkeypatch,
        "https://shop.example.com/products/item",
        [
            {"id": 1, "price": "10.00", "available": False},
            {"id": 2, "price": None, "available": True},
        ],
    )
    assert result.price is None
    assert result.available is not False


async def test_shopify_unrelated_embedded_price_is_not_product_price():
    result = ShopifyScraper()._read_product_page(
        '<script>var recommendation={"price":7500};</script>',
        "https://shop.example.com/products/item",
    )
    assert result is None or result.price is None


async def test_shopify_redirect_to_home_is_not_invented_404(monkeypatch):
    response = httpx.Response(
        200, text="<title>Home</title>", request=httpx.Request("GET", "https://shop.example.com/")
    )
    monkeypatch.setattr(shopify, "_fetch_shopify_response", AsyncMock(return_value=response))
    async with httpx.AsyncClient() as client:
        result = await ShopifyScraper._fetch_html("https://shop.example.com/products/item", client)
    assert result is None


async def _generic_result(monkeypatch, payload) -> ProductInfo:
    html = '<script type="application/ld+json">' + json.dumps(payload) + "</script>"
    monkeypatch.setattr(generic, "_fetch_generic_html", AsyncMock(return_value=html))
    async with httpx.AsyncClient() as client:
        return await GenericScraper().scrape("https://shop.example.com/item", client)


async def test_generic_rejects_jsonld_explicitly_owned_by_other_product(monkeypatch):
    result = await _generic_result(
        monkeypatch,
        {
            "@type": "Product",
            "url": "https://shop.example.com/other",
            "name": "Other",
            "offers": {"price": "7.00", "priceCurrency": "EUR"},
        },
    )
    assert result.price is None


async def test_generic_reports_selected_jsonld_offer_out_of_stock(monkeypatch):
    result = await _generic_result(
        monkeypatch,
        {
            "@type": "Product",
            "url": "https://shop.example.com/item",
            "name": "Item",
            "offers": {
                "price": "7.00",
                "priceCurrency": "EUR",
                "availability": "https://schema.org/OutOfStock",
            },
        },
    )
    assert result.available is False


async def test_amazon_rejects_page_redirected_to_different_asin(monkeypatch):
    other = "https://www.amazon.com/dp/B087654321"
    html = ImagePageHTML(
        f'<link rel="canonical" href="{other}"><h1 id="productTitle">Other</h1>'
        '<span class="priceToPay"><span class="a-offscreen">$12.00</span></span>',
        other,
    )
    monkeypatch.setattr(amazon, "_fetch_amazon_page", AsyncMock(return_value=html))
    async with httpx.AsyncClient() as client:
        result = await AmazonScraper().scrape("https://www.amazon.com/dp/B012345678", client)
    assert result.price is None


async def test_amazon_preserves_currency_of_price(monkeypatch):
    url = "https://www.amazon.com/dp/B012345678"
    html = ImagePageHTML(
        '<h1 id="productTitle">Item</h1><span class="priceToPay">'
        '<span class="a-offscreen">USD 12.00</span></span>',
        url,
    )
    monkeypatch.setattr(amazon, "_fetch_amazon_page", AsyncMock(return_value=html))
    async with httpx.AsyncClient() as client:
        result = await AmazonScraper().scrape(url, client)
    assert result.price == Decimal("12.00")
    assert result.currency == "USD"


async def test_fetch_treats_404_captcha_as_block():
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'<form id="captcha"></form>'

    async def respond(request):
        return httpx.Response(404, stream=Body(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CaptchaDetected):
            await _fetch_primary(
                "https://example.com/item", client, hdrs={}, max_redirects=3, max_bytes=10000
            )


async def test_amazon_ignores_jsonld_for_different_asin(monkeypatch):
    url = "https://www.amazon.com/dp/B012345678"
    other = "https://www.amazon.com/dp/B087654321"
    data = {"@type": "Product", "url": other, "offers": {"price": "2.00", "priceCurrency": "USD"}}
    html = ImagePageHTML(
        '<h1 id="productTitle">Item</h1><span class="priceToPay">'
        '<span class="a-offscreen">USD 12.00</span></span><script type="application/ld+json">'
        + json.dumps(data)
        + "</script>",
        url,
    )
    monkeypatch.setattr(amazon, "_fetch_amazon_page", AsyncMock(return_value=html))
    async with httpx.AsyncClient() as client:
        result = await AmazonScraper().scrape(url, client)
    assert result.price == Decimal("12.00")


@pytest.mark.parametrize("price_text", ["$12.00", "¥1200", "kr 120.00"])
async def test_amazon_refuses_ambiguous_currency_symbol(monkeypatch, price_text):
    url = "https://www.amazon.com/dp/B012345678"
    html = ImagePageHTML(
        '<h1 id="productTitle">Item</h1><span class="priceToPay">'
        f'<span class="a-offscreen">{price_text}</span></span>',
        url,
    )
    monkeypatch.setattr(amazon, "_fetch_amazon_page", AsyncMock(return_value=html))
    async with httpx.AsyncClient() as client:
        result = await AmazonScraper().scrape(url, client)
    assert result.price is None
    assert result.error == "currency_ambiguous"
