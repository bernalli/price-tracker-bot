"""Unit tests for TargetScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers import target as target_module
from price_tracker.scrapers.target import TargetScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_target_handles_target_domain() -> None:
    scraper = TargetScraper()
    assert scraper.can_handle("https://www.target.com/p/-/A-1234")


def test_target_rejects_unrelated() -> None:
    scraper = TargetScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_target_priority_50() -> None:
    assert TargetScraper.priority == 50


@pytest.mark.asyncio
async def test_target_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("target/sample_product.html")
    scraper = TargetScraper()
    url = "https://www.target.com/p/-/A-1234"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency in ("USD", "EUR", "GBP", "CAD")
    assert info.name


@pytest.mark.asyncio
async def test_target_returns_error_on_missing_data() -> None:
    scraper = TargetScraper()
    url = "https://www.target.com/p/-/A-9999"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_target_jsonld_fallback_when_no_dom(
    load_fixture: Callable[[str], str],
) -> None:
    """Fixture with only JSON-LD (no DOM data-test) → JSON-LD fallback yields price."""
    html = load_fixture("target/sample_target_fallback.html")
    scraper = TargetScraper()
    url = "https://www.target.com/p/-/A-1235"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("12.49")
    assert info.currency == "USD"
    assert info.name == "Fallback Target Product"
    assert info.error is None


@pytest.mark.asyncio
async def test_target_returns_error_on_http_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx response (after retry exhaustion) → ProductInfo with HTTP error."""
    scraper = TargetScraper()

    async def _fast_fetch(url: str, client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(url)

    monkeypatch.setattr(target_module, "_fetch_target_html", _fast_fetch)

    url = "https://www.target.com/p/-/A-5555"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(500)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
    assert "HTTP error" in info.error


@pytest.mark.asyncio
async def test_target_handles_malformed_jsonld_gracefully() -> None:
    """Malformed JSON-LD does not crash; price still extracted from DOM."""
    scraper = TargetScraper()
    url = "https://www.target.com/p/-/A-7777"
    html = """
    <html><head><title>Sample</title></head><body>
    <script type="application/ld+json">{not valid json</script>
    <span data-test="product-price">$8.50</span>
    </body></html>
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("8.50")
    assert info.currency == "USD"
