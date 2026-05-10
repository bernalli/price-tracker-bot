"""Unit tests for MediamarktScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers import mediamarkt as mediamarkt_module
from price_tracker.scrapers.mediamarkt import MediamarktScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_mediamarkt_handles_de_domain() -> None:
    scraper = MediamarktScraper()
    assert scraper.can_handle("https://www.mediamarkt.de/de/product/_sample-1234.html")


def test_mediamarkt_handles_it_domain() -> None:
    scraper = MediamarktScraper()
    assert scraper.can_handle("https://www.mediamarkt.it/it/product/_sample-1234.html")


def test_mediamarkt_handles_ch_domain() -> None:
    scraper = MediamarktScraper()
    assert scraper.can_handle("https://www.mediamarkt.ch/de/product/_sample-1234.html")


def test_mediamarkt_rejects_unrelated() -> None:
    scraper = MediamarktScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_mediamarkt_priority_50() -> None:
    assert MediamarktScraper.priority == 50


@pytest.mark.asyncio
async def test_mediamarkt_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("mediamarkt/sample_product.html")
    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.de/de/product/_sample-1234.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency in ("EUR", "USD", "CHF", "PLN", "HUF")
    assert info.name


@pytest.mark.asyncio
async def test_mediamarkt_default_currency_ch_is_chf() -> None:
    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.ch/de/product/_sample-9999.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body><h1>x</h1></body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.currency == "CHF"
    assert info.error is not None


@pytest.mark.asyncio
async def test_mediamarkt_returns_error_on_missing_data() -> None:
    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.de/de/product/_missing-9999.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_mediamarkt_dom_fallback_when_no_jsonld(
    load_fixture: Callable[[str], str],
) -> None:
    """Fixture with only DOM data-test (no JSON-LD) → DOM fallback yields price."""
    html = load_fixture("mediamarkt/sample_mediamarkt_fallback.html")
    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.de/de/product/_fallback-1234.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("449.00")
    # Currency falls back to EUR via _default_currency_for_url(.de)
    assert info.currency == "EUR"
    assert info.error is None


@pytest.mark.asyncio
async def test_mediamarkt_returns_error_on_http_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx response → ProductInfo with HTTP error."""
    scraper = MediamarktScraper()

    async def _fast_fetch(url: str, client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(url)

    monkeypatch.setattr(mediamarkt_module, "_fetch_mediamarkt_html", _fast_fetch)

    url = "https://www.mediamarkt.de/de/product/_error-5555.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(500)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
    assert "HTTP error" in info.error


@pytest.mark.asyncio
async def test_mediamarkt_default_currency_pl_is_pln() -> None:
    """PL TLD → default currency PLN when no JSON-LD/DOM price found."""
    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.pl/pl/product/_sample-9999.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body><h1>x</h1></body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.currency == "PLN"
    assert info.error is not None
