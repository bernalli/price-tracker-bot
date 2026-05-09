"""Unit tests for EbayScraper (price parsing, error handling, can_handle)."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers import ebay as ebay_module
from price_tracker.scrapers.ebay import EbayScraper

if TYPE_CHECKING:
    from collections.abc import Callable


# ── can_handle ────────────────────────────────────────────────────


def test_ebay_can_handle_all_locales():
    scraper = EbayScraper()
    for tld in ["com", "it", "de", "co.uk", "fr", "es", "nl", "pl", "com.au", "ca"]:
        assert scraper.can_handle(f"https://www.ebay.{tld}/itm/123"), f"should handle ebay.{tld}"


def test_ebay_rejects_other_domains():
    scraper = EbayScraper()
    for url in [
        "https://www.amazon.com/dp/B01",
        "https://www.etsy.com/listing/1",
        "https://shop.example.com/p/abc",
        "https://fakeebaysite.com/itm/1",
    ]:
        assert not scraper.can_handle(url), f"should NOT handle {url}"


def test_ebay_priority():
    assert EbayScraper.priority == 90


# ── scrape: happy path ────────────────────────────────────────────


@pytest.mark.asyncio()
async def test_ebay_parses_fixture_html(load_fixture: Callable[[str], str]) -> None:
    """Fixture has both JSON-LD (preferred) and microdata for price 29.99 EUR."""
    html = load_fixture("ebay/sample_product.html")
    scraper = EbayScraper()

    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.ebay.com/itm/SAMPLE001").respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape("https://www.ebay.com/itm/SAMPLE001", client)

    assert info.price == Decimal("29.99")
    assert info.currency == "EUR"
    assert info.name == "Sample Product"
    assert info.error is None


# ── scrape: error paths ──────────────────────────────────────────


@pytest.mark.asyncio()
async def test_ebay_handles_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-retryable 404 → ProductInfo with error, price=None, no crash."""
    scraper = EbayScraper()

    # Bypass retry for fast test execution.
    async def _fast_fetch(url: str, client: httpx.AsyncClient) -> str:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

    monkeypatch.setattr(ebay_module, "_fetch_ebay_html", _fast_fetch)

    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.ebay.com/itm/MISSING").respond(404)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape("https://www.ebay.com/itm/MISSING", client)

    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio()
async def test_ebay_handles_429_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retryable 429 → after retries exhausted, return ProductInfo with error."""
    scraper = EbayScraper()

    async def _fast_fail(url: str, client: httpx.AsyncClient) -> str:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

    monkeypatch.setattr(ebay_module, "_fetch_ebay_html", _fast_fail)

    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.ebay.com/itm/RATE").respond(429)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape("https://www.ebay.com/itm/RATE", client)

    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio()
async def test_ebay_missing_price_selectors() -> None:
    """HTML without any price → error 'Prezzo non trovato', no crash."""
    scraper = EbayScraper()
    html_no_price = """
    <!DOCTYPE html><html><body>
    <h1 class="x-item-title__mainTitle"><span>Stripped Item</span></h1>
    </body></html>
    """

    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.ebay.com/itm/NOPRICE").respond(200, text=html_no_price)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape("https://www.ebay.com/itm/NOPRICE", client)

    assert info.price is None
    assert info.error is not None
