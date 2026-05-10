"""Unit tests for WalmartScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers import walmart as walmart_module
from price_tracker.scrapers.walmart import WalmartScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_walmart_handles_walmart_domain() -> None:
    scraper = WalmartScraper()
    assert scraper.can_handle("https://www.walmart.com/ip/123")


def test_walmart_rejects_unrelated() -> None:
    scraper = WalmartScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_walmart_priority_50() -> None:
    assert WalmartScraper.priority == 50


@pytest.mark.asyncio
async def test_walmart_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("walmart/sample_product.html")
    scraper = WalmartScraper()
    url = "https://www.walmart.com/ip/123"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency in ("USD", "EUR", "GBP", "CAD")
    assert info.name


@pytest.mark.asyncio
async def test_walmart_returns_error_on_missing_data() -> None:
    scraper = WalmartScraper()
    url = "https://www.walmart.com/ip/999"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_walmart_microdata_fallback_when_no_jsonld(
    load_fixture: Callable[[str], str],
) -> None:
    """Fixture with only microdata (no JSON-LD) → microdata fallback yields price."""
    html = load_fixture("walmart/sample_walmart_fallback.html")
    scraper = WalmartScraper()
    url = "https://www.walmart.com/ip/2025"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("19.97")
    assert info.currency == "USD"
    assert info.name == "Fallback Walmart Product"
    assert info.error is None


@pytest.mark.asyncio
async def test_walmart_returns_error_on_http_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx response → ProductInfo with HTTP error (no retry overhead in tests)."""
    scraper = WalmartScraper()

    async def _fast_fetch(url: str, client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(url)

    monkeypatch.setattr(walmart_module, "_fetch_walmart_html", _fast_fetch)

    url = "https://www.walmart.com/ip/5555"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(500)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
    assert "HTTP error" in info.error


@pytest.mark.asyncio
async def test_walmart_handles_malformed_jsonld_falls_back_to_microdata() -> None:
    """Invalid JSON-LD does not crash; microdata fallback yields price."""
    scraper = WalmartScraper()
    url = "https://www.walmart.com/ip/7777"
    html = """
    <html><head><title>Sample</title>
    <script type="application/ld+json">not valid json {{{</script>
    </head><body>
    <h1 itemprop="name">Sample Walmart</h1>
    <span itemprop="price" content="5.99">$5.99</span>
    <meta itemprop="priceCurrency" content="USD" />
    </body></html>
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("5.99")
    assert info.currency == "USD"
