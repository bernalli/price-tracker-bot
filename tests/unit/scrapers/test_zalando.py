"""Unit tests for ZalandoScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers import zalando as zalando_module
from price_tracker.scrapers.zalando import ZalandoScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_zalando_handles_it_domain() -> None:
    scraper = ZalandoScraper()
    assert scraper.can_handle("https://www.zalando.it/sample-shoe-12345/")


def test_zalando_handles_de_domain() -> None:
    scraper = ZalandoScraper()
    assert scraper.can_handle("https://www.zalando.de/sample-shoe-12345/")


def test_zalando_handles_co_uk_domain() -> None:
    scraper = ZalandoScraper()
    assert scraper.can_handle("https://www.zalando.co.uk/sample-shoe-12345/")


def test_zalando_handles_ch_domain() -> None:
    scraper = ZalandoScraper()
    assert scraper.can_handle("https://www.zalando.ch/sample-shoe-12345/")


def test_zalando_rejects_unrelated() -> None:
    scraper = ZalandoScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_zalando_priority_50() -> None:
    assert ZalandoScraper.priority == 50


@pytest.mark.asyncio
async def test_zalando_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("zalando/sample_product.html")
    scraper = ZalandoScraper()
    url = "https://www.zalando.it/sample-shoe-12345/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency == "EUR"
    assert info.name


@pytest.mark.asyncio
async def test_zalando_default_currency_uk_is_gbp() -> None:
    scraper = ZalandoScraper()
    url = "https://www.zalando.co.uk/missing-product-9999/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body><h1>x</h1></body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.currency == "GBP"
    assert info.error is not None


@pytest.mark.asyncio
async def test_zalando_returns_error_on_missing_data() -> None:
    scraper = ZalandoScraper()
    url = "https://www.zalando.it/missing-product-9999/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_zalando_dom_fallback_when_no_jsonld(
    load_fixture: Callable[[str], str],
) -> None:
    """Fixture with only DOM data-testid (no JSON-LD) → DOM fallback yields price+EUR."""
    html = load_fixture("zalando/sample_zalando_fallback.html")
    scraper = ZalandoScraper()
    url = "https://www.zalando.it/sample-fallback-12345/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("59.90")
    assert info.currency == "EUR"
    assert info.error is None


@pytest.mark.asyncio
async def test_zalando_returns_error_on_http_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx response → ProductInfo with HTTP error."""
    scraper = ZalandoScraper()

    async def _fast_fetch(url: str, client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(url)

    monkeypatch.setattr(zalando_module, "_fetch_zalando_html", _fast_fetch)

    url = "https://www.zalando.it/error-product-5555/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(500)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
    assert "HTTP error" in info.error


@pytest.mark.asyncio
async def test_zalando_dom_fallback_gbp_on_uk_tld() -> None:
    """DOM fallback recognises £ in price text and maps to GBP."""
    scraper = ZalandoScraper()
    url = "https://www.zalando.co.uk/sample-uk-12345/"
    html = """
    <html><head><title>Zalando UK</title></head><body>
    <h1>Zalando UK</h1>
    <span data-testid="current-price">&pound;39.95</span>
    </body></html>
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("39.95")
    assert info.currency == "GBP"
