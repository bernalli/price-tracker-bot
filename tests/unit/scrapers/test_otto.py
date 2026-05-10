"""Unit tests for OttoScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers import otto as otto_module
from price_tracker.scrapers.otto import OttoScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_otto_handles_otto_de() -> None:
    scraper = OttoScraper()
    assert scraper.can_handle("https://www.otto.de/p/sample-12345/")


def test_otto_rejects_non_otto() -> None:
    scraper = OttoScraper()
    assert not scraper.can_handle("https://www.amazon.de/dp/B01")


def test_otto_priority_50() -> None:
    assert OttoScraper.priority == 50


@pytest.mark.asyncio
async def test_otto_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("otto/sample_product.html")
    scraper = OttoScraper()
    url = "https://www.otto.de/p/sample-12345/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency == "EUR"
    assert info.name


@pytest.mark.asyncio
async def test_otto_returns_error_on_missing_data() -> None:
    scraper = OttoScraper()
    url = "https://www.otto.de/p/missing-9999/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_otto_dom_fallback_when_no_jsonld(
    load_fixture: Callable[[str], str],
) -> None:
    """Fixture with only DOM .pdp_price__main-price (no JSON-LD) → DOM fallback yields price+EUR."""
    html = load_fixture("otto/sample_otto_fallback.html")
    scraper = OttoScraper()
    url = "https://www.otto.de/p/fallback-12345/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("79.99")
    assert info.currency == "EUR"
    assert info.error is None


@pytest.mark.asyncio
async def test_otto_returns_error_on_http_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx response → ProductInfo with HTTP error."""
    scraper = OttoScraper()

    async def _fast_fetch(url: str, client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(url)

    monkeypatch.setattr(otto_module, "_fetch_otto_html", _fast_fetch)

    url = "https://www.otto.de/p/error-5555/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(500)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
    assert "HTTP error" in info.error


@pytest.mark.asyncio
async def test_otto_handles_malformed_jsonld_falls_back_to_dom() -> None:
    """Invalid JSON-LD doesn't crash; DOM fallback still extracts the price."""
    scraper = OttoScraper()
    url = "https://www.otto.de/p/malformed-jsonld/"
    html = """
    <html><head><title>Otto Sample</title>
    <script type="application/ld+json">{not valid json{{</script>
    </head><body>
    <h1>Otto Sample</h1>
    <span class="pdp_price__main-price">12,99 &euro;</span>
    </body></html>
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("12.99")
    assert info.currency == "EUR"
