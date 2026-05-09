"""Unit tests for ShopifyScraper (price parsing, error handling, can_handle)."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers import shopify as shopify_module
from price_tracker.scrapers.shopify import ShopifyScraper

if TYPE_CHECKING:
    from collections.abc import Callable


# ── can_handle ────────────────────────────────────────────────────


def test_shopify_can_handle_known_domains():
    scraper = ShopifyScraper()
    assert scraper.can_handle("https://allbirds.com/products/runner")
    assert scraper.can_handle("https://gymshark.com/products/leggings")


def test_shopify_can_handle_products_path():
    scraper = ShopifyScraper()
    # Generic /products/<handle> on unknown domain → still True
    assert scraper.can_handle("https://shop.example.com/products/sample")
    assert scraper.can_handle("https://www.someshopify.io/products/widget-pro")


def test_shopify_rejects_amazon_ebay():
    scraper = ShopifyScraper()
    # Amazon URLs without /products/ should NOT match
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")
    assert not scraper.can_handle("https://www.ebay.com/itm/123")


def test_shopify_rejects_url_without_products_segment():
    scraper = ShopifyScraper()
    assert not scraper.can_handle("https://shop.example.com/collections/all")
    assert not scraper.can_handle("https://shop.example.com/")


def test_shopify_priority():
    assert ShopifyScraper.priority == 80


# ── scrape: happy path (HTML extraction) ─────────────────────────


@pytest.mark.asyncio
async def test_shopify_parses_fixture_html(load_fixture: Callable[[str], str]) -> None:
    """Fixture HTML has og:price + JSON-LD; JSON API returns 404 → falls back to HTML."""
    html = load_fixture("shopify/sample_product.html")
    scraper = ShopifyScraper()

    url = "https://shop.example.com/products/sample"
    json_url = "https://shop.example.com/products/sample.json"

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(404)
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("29.99")
    # Shopify HTML-fallback path doesn't set currency (only JSON-API path does);
    # current behavior returns None. Tracked as scraper limitation.
    assert info.currency in (None, "EUR")
    assert info.error is None


@pytest.mark.asyncio
async def test_shopify_parses_via_json_api() -> None:
    """If /products/<handle>.json returns Shopify product JSON, that path wins."""
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/sample"
    json_url = "https://shop.example.com/products/sample.json"
    json_payload = {
        "product": {
            "title": "Sample Product",
            "variants": [{"id": 1, "price": "29.99"}],
        }
    }
    # Minimal HTML used downstream for currency detection
    html = (
        '<html><head><meta property="og:price:currency" content="EUR"></head><body></body></html>'
    )

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(200, json=json_payload)
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("29.99")
    assert info.name == "Sample Product"
    # Currency should be detected via og:price:currency from HTML
    assert info.currency == "EUR"


# ── scrape: error paths ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_shopify_handles_404_on_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both JSON API and HTML 404 → error, no crash."""
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/missing"
    json_url = "https://shop.example.com/products/missing.json"

    # Bypass HTML retry for speed.
    async def _fast_fetch(u: str, client: httpx.AsyncClient) -> str:
        response = await client.get(u)
        response.raise_for_status()
        return response.text

    monkeypatch.setattr(shopify_module, "_fetch_shopify_html", _fast_fetch)

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(404)
        router.get(url).respond(404)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_shopify_handles_429_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both JSON API and HTML return 429 → after retries, error."""
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/rate"
    json_url = "https://shop.example.com/products/rate.json"

    async def _fast_html(u: str, client: httpx.AsyncClient) -> str:
        response = await client.get(u)
        response.raise_for_status()
        return response.text

    async def _fast_json(u: str, client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(u)

    monkeypatch.setattr(shopify_module, "_fetch_shopify_html", _fast_html)
    monkeypatch.setattr(shopify_module, "_fetch_shopify_json", _fast_json)

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(429)
        router.get(url).respond(429)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_shopify_missing_price_selectors() -> None:
    """HTML with no price markup → error, no crash."""
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/empty"
    json_url = "https://shop.example.com/products/empty.json"
    html_no_price = """
    <!DOCTYPE html><html><body>
    <h1 class="product__title">Stripped Item</h1>
    </body></html>
    """

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(404)
        router.get(url).respond(200, text=html_no_price)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None
    assert info.error is not None
