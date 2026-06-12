"""Unit tests for EtsyScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers import etsy as etsy_module
from price_tracker.scrapers.etsy import EtsyScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_etsy_handles_etsy_domain() -> None:
    scraper = EtsyScraper()
    assert scraper.can_handle("https://www.etsy.com/listing/123/sample")


def test_etsy_rejects_unrelated() -> None:
    scraper = EtsyScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_etsy_priority_50() -> None:
    assert EtsyScraper.priority == 50


@pytest.mark.asyncio
async def test_etsy_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("etsy/sample_product.html")
    scraper = EtsyScraper()
    url = "https://www.etsy.com/listing/123/sample"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency in ("USD", "EUR", "GBP", "CAD")
    assert info.name


@pytest.mark.asyncio
async def test_etsy_returns_error_on_missing_data() -> None:
    scraper = EtsyScraper()
    url = "https://www.etsy.com/listing/999/missing"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_etsy_dom_fallback_when_no_jsonld(
    load_fixture: Callable[[str], str],
) -> None:
    """Fixture with only DOM data-buy-box (no JSON-LD) → DOM fallback yields price."""
    html = load_fixture("etsy/sample_etsy_fallback.html")
    scraper = EtsyScraper()
    url = "https://www.etsy.com/listing/124/fallback"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("14.25")
    assert info.currency == "USD"
    assert info.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original_block",
    [
        '<p><s><span class="currency-symbol">CA$</span>'
        '<span class="currency-value">99.00</span></s></p>',
        '<p class="wt-text-strikethrough"><span class="currency-symbol">CA$</span>'
        '<span class="currency-value">99.00</span></p>',
    ],
)
async def test_etsy_dom_skips_strikethrough_original_price(original_block: str) -> None:
    """Repro (#40): discounted buy-box lists the struck-through original price
    first in document order; the sale price (and its adjacent symbol) must win."""
    scraper = EtsyScraper()
    url = "https://www.etsy.com/listing/4040/discounted"
    html = f"""
    <html><head><title>Discounted Listing - Etsy</title></head><body>
    <div data-buy-box-listing-price>
      {original_block}
      <p><span class="currency-symbol">$</span><span class="currency-value">49.00</span></p>
    </div>
    </body></html>
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("49.00")
    assert info.currency == "USD"


@pytest.mark.asyncio
async def test_etsy_dom_keeps_struck_value_when_it_is_the_only_one() -> None:
    """Odd markup where the only .currency-value sits in <s>: the strikethrough
    filter empties, so fall back to the first value instead of no price (#40)."""
    scraper = EtsyScraper()
    url = "https://www.etsy.com/listing/4041/odd-markup"
    html = """
    <html><head><title>Odd Listing - Etsy</title></head><body>
    <div data-buy-box-listing-price>
      <p><s><span class="currency-symbol">$</span><span class="currency-value">99.00</span></s></p>
    </div>
    </body></html>
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("99.00")
    assert info.currency == "USD"


@pytest.mark.asyncio
async def test_etsy_returns_error_on_http_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx response → ProductInfo with HTTP error."""
    scraper = EtsyScraper()

    async def _fast_fetch(url: str, client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(url)

    monkeypatch.setattr(etsy_module, "_fetch_etsy_html", _fast_fetch)

    url = "https://www.etsy.com/listing/5555/sample"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(500)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
    assert "HTTP error" in info.error


@pytest.mark.asyncio
async def test_etsy_handles_malformed_jsonld_falls_back_to_dom() -> None:
    """Invalid JSON-LD does not crash; DOM fallback still extracts price."""
    scraper = EtsyScraper()
    url = "https://www.etsy.com/listing/7777/sample"
    html = """
    <html><head><title>Sample</title>
    <script type="application/ld+json">not valid json {{{</script>
    </head><body>
    <h1>Etsy Sample</h1>
    <div data-buy-box-listing-price>
      <p><span class="currency-symbol">$</span><span class="currency-value">7.50</span></p>
    </div>
    </body></html>
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("7.50")
    assert info.currency == "USD"
