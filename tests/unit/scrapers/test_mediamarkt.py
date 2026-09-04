"""Unit tests for MediamarktScraper."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from bs4 import BeautifulSoup

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


# ── Regressions from the live mediamarkt.es page (SKU 1667552) ───────────
#
# Three independent defects left this product priceless: the Product moved
# inside a BuyAction, the DOM wrapper the fallback selected disappeared, and
# the shared financing filter threw the real offer away.


@pytest.mark.asyncio
async def test_mediamarkt_reads_price_from_buyaction_wrapped_jsonld(
    load_fixture: Callable[[str], str],
) -> None:
    """Current pages nest the Product under BuyAction.object, not at top level."""
    html = load_fixture("mediamarkt/sample_product_es_buyaction.html")
    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.es/es/product/_monitor-1667552.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("259")
    assert info.currency == "EUR"
    assert info.error is None
    # Name comes from the JSON-LD Product, not the <title> fallback.
    assert info.name is not None
    assert info.name.startswith("Monitor gaming - MSI MAG 274QPF X32")


@pytest.mark.asyncio
async def test_mediamarkt_dom_fallback_reads_loose_branded_price_spans(
    load_fixture: Callable[[str], str],
) -> None:
    """DOM fallback works without the [data-test=mms-pdp-product-price] wrapper.

    Also covers the dash-decimal rendering of a round price ("259,–"), which
    must not be read as 259.0 with a bogus decimal part.
    """
    html = load_fixture("mediamarkt/sample_product_es_buyaction.html")
    # Strip the JSON-LD so only the DOM strategy can answer.
    html_no_jsonld = re.sub(
        r'<script type="application/ld\+json">.*?</script>', "", html, flags=re.DOTALL
    )
    assert "mms-pdp-product-price" not in html_no_jsonld
    assert "branded-price-whole-value" in html_no_jsonld

    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.es/es/product/_monitor-1667552.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html_no_jsonld)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("259")
    assert info.currency == "EUR"
    assert info.error is None


def test_mediamarkt_dom_joins_whole_and_decimal_spans() -> None:
    """Real decimals are joined onto the whole value; a dash is not."""
    scraper = MediamarktScraper()

    def _price_html(decimals: str) -> str:
        return (
            "<html><body><div>"
            '<span data-test="branded-price-whole-value">1.299,</span>'
            f'<div><span data-test="branded-price-decimal-value">{decimals}</span></div>'
            '<span data-test="branded-price-currency"> €</span>'
            "</div></body></html>"
        )

    with_decimals = scraper._try_dom(BeautifulSoup(_price_html("99"), "lxml"))
    assert with_decimals is not None
    assert with_decimals["price"] == Decimal("1299.99")

    round_price = scraper._try_dom(BeautifulSoup(_price_html("–"), "lxml"))
    assert round_price is not None
    assert round_price["price"] == Decimal("1299")


# ── Both page shapes must keep working ────────────────────────────────────


@pytest.mark.asyncio
async def test_mediamarkt_still_reads_top_level_product_jsonld(
    load_fixture: Callable[[str], str],
) -> None:
    """The pre-BuyAction shape (Product at the JSON-LD root) still resolves."""
    html = load_fixture("mediamarkt/sample_product.html")
    assert '"@type": "Product"' in html
    assert "BuyAction" not in html
    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.de/de/product/_sample-1234.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("799.00")
    assert info.error is None


def test_mediamarkt_dom_accepts_legacy_wrapper_and_bare_spans() -> None:
    """The old [data-test=mms-pdp-product-price] wrapper and its absence both work."""
    scraper = MediamarktScraper()
    span = '<span data-test="branded-price-whole-value">449,00</span>'

    legacy = f'<html><body><div data-test="mms-pdp-product-price">{span}</div></body></html>'
    bare = f"<html><body><div>{span}</div></body></html>"

    for label, markup in (("legacy", legacy), ("bare", bare)):
        result = scraper._try_dom(BeautifulSoup(markup, "lxml"))
        assert result is not None, label
        assert result["price"] == Decimal("449.00"), label


def test_mediamarkt_dom_prefers_wrapper_over_earlier_carousel_price() -> None:
    """A recommendations price before the main block must not win.

    Dropping the wrapper as an anchor is what makes this reachable: the fallback
    now scans the document, so the main product's block has to stay preferred.
    """
    scraper = MediamarktScraper()
    html = (
        "<html><body>"
        '<div class="product-carousel">'
        '<span data-test="branded-price-whole-value">19,99</span>'
        "</div>"
        '<div data-test="mms-pdp-product-price">'
        '<span data-test="branded-price-whole-value">449,00</span>'
        "</div>"
        "</body></html>"
    )
    result = scraper._try_dom(BeautifulSoup(html, "lxml"))
    assert result is not None
    assert result["price"] == Decimal("449.00")


def test_mediamarkt_dom_skips_carousel_price_without_wrapper() -> None:
    """With no wrapper at all, a carousel price is skipped for the real one."""
    scraper = MediamarktScraper()
    html = (
        "<html><body>"
        '<aside class="recommendations">'
        '<span data-test="branded-price-whole-value">19,99</span>'
        "</aside>"
        '<div><span data-test="branded-price-whole-value">449,00</span></div>'
        "</body></html>"
    )
    result = scraper._try_dom(BeautifulSoup(html, "lxml"))
    assert result is not None
    assert result["price"] == Decimal("449.00")
