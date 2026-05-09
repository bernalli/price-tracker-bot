"""Unit tests for GenericScraper (multi-strategy extraction, error paths)."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers import generic as generic_module
from price_tracker.scrapers.generic import GenericScraper

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def _disable_curl_cffi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force GenericScraper to use httpx (so respx can intercept)."""

    async def _no_curl(url: str) -> str | None:
        return None

    monkeypatch.setattr(generic_module, "_fetch_with_curl_cffi", _no_curl)


# ── can_handle ────────────────────────────────────────────────────


def test_generic_handles_anything():
    scraper = GenericScraper()
    assert scraper.can_handle("https://random-site.example.com/product")
    assert scraper.can_handle("https://www.somestore.io/p/123")
    assert scraper.can_handle("https://shop.example.com/products/abc")


def test_generic_priority_zero():
    assert GenericScraper.priority == 0


# ── scrape: happy path with JSON-LD fixture ──────────────────────


@pytest.mark.asyncio
async def test_generic_parses_jsonld_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    """JSON-LD Product+Offer should yield price=29.99 EUR."""
    html = load_fixture("generic/sample_jsonld.html")
    scraper = GenericScraper()
    url = "https://example.com/product/jsonld"

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("29.99")
    assert info.currency == "EUR"
    assert info.error is None


@pytest.mark.asyncio
async def test_generic_parses_microdata_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    """Microdata Product+Offer should yield price=29.99 EUR."""
    html = load_fixture("generic/sample_microdata.html")
    scraper = GenericScraper()
    url = "https://example.com/product/microdata"

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("29.99")
    assert info.currency == "EUR"
    assert info.error is None


# ── scrape: error paths ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_generic_handles_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 → curl_cffi None → httpx 404 → ProductInfo with error."""
    scraper = GenericScraper()

    async def _fast_fetch(url: str, client: httpx.AsyncClient) -> str:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

    monkeypatch.setattr(generic_module, "_fetch_generic_html", _fast_fetch)

    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/missing").respond(404)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape("https://example.com/missing", client)

    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_generic_handles_429_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 with retries exhausted → ProductInfo with error."""
    scraper = GenericScraper()

    async def _fast_fail(url: str, client: httpx.AsyncClient) -> str:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

    monkeypatch.setattr(generic_module, "_fetch_generic_html", _fast_fail)

    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/rate").respond(429)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape("https://example.com/rate", client)

    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_generic_missing_price_selectors() -> None:
    """HTML lacking any price markup → error 'Prezzo non trovato', no crash."""
    scraper = GenericScraper()
    html_no_price = """
    <!DOCTYPE html><html><head><title>Empty Page</title></head>
    <body><p>No prices anywhere here.</p></body></html>
    """

    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/empty").respond(200, text=html_no_price)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape("https://example.com/empty", client)

    assert info.price is None
    assert info.error is not None
