"""Unit tests for GenericScraper (multi-strategy extraction, error paths)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from bs4 import BeautifulSoup

from price_tracker.scrapers import generic as generic_module
from price_tracker.scrapers.generic import GenericScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def _jsonld_soup(payload: object) -> BeautifulSoup:
    html = (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(payload)}"
        "</script></head><body></body></html>"
    )
    return BeautifulSoup(html, "lxml")


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


# ── JSON-LD offers selection (#27) ────────────────────────────────


def test_jsonld_offers_picks_main_price_not_cheapest_addon() -> None:
    """Offer list [main 199.99, addon 14.99] → main price, not the cheapest entry."""
    payload = {
        "@type": "Product",
        "name": "Laptop Pro",
        "offers": [
            {"@type": "Offer", "price": "199.99", "priceCurrency": "EUR"},
            {
                "@type": "Offer",
                "name": "Garanzia aggiuntiva",
                "price": "14.99",
                "priceCurrency": "EUR",
            },
        ],
    }
    result = GenericScraper()._try_json_ld(_jsonld_soup(payload))
    assert result is not None
    assert result["price"] == Decimal("199.99")
    assert result["currency"] == "EUR"


def test_jsonld_offers_filters_financing_offer() -> None:
    """A '24 Raten' monthly-installment offer must not be selected as the price."""
    payload = {
        "@type": "Product",
        "name": "TV 55",
        "offers": [
            {
                "@type": "Offer",
                "name": "Finanzierung: 24 Raten",
                "price": "24.99",
                "priceSpecification": {
                    "@type": "UnitPriceSpecification",
                    "billingDuration": 24,
                },
            },
            {"@type": "Offer", "price": "199.99", "priceCurrency": "EUR"},
        ],
    }
    result = GenericScraper()._try_json_ld(_jsonld_soup(payload))
    assert result is not None
    assert result["price"] == Decimal("199.99")


def test_jsonld_offer_without_currency_yields_none_not_eur() -> None:
    """Missing priceCurrency must not silently default to EUR."""
    payload = {
        "@type": "Product",
        "name": "Widget",
        "offers": {"@type": "Offer", "price": "49.99"},
    }
    result = GenericScraper()._try_json_ld(_jsonld_soup(payload))
    assert result is not None
    assert result["price"] == Decimal("49.99")
    assert result.get("currency") is None


# ── JS product data (#53) ────────────────────────────────────────


def _script_soup(js: str) -> BeautifulSoup:
    return BeautifulSoup(f"<html><head><script>{js}</script></head><body></body></html>", "lxml")


def test_js_product_data_skips_unidentified_object_before_product() -> None:
    """An analytics/related object's price must not win over the product object."""
    js = (
        'dataLayer.push({"listName": "related-products", "price": 9.99});'
        'var product = {"name": "Laptop Pro", "sku": "LP-100", "price": 199.99};'
    )
    result = GenericScraper()._try_js_product_data(_script_soup(js))
    assert result is not None
    assert result["price"] == Decimal("199.99")


def test_js_product_data_bare_price_fallback_still_works() -> None:
    """With no product-identified object anywhere, the bare regex fallback applies."""
    js = 'window.__app = {"currency": "EUR", "price": 49.99};'
    result = GenericScraper()._try_js_product_data(_script_soup(js))
    assert result is not None
    assert result["price"] == Decimal("49.99")


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
async def test_generic_handles_500_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-block 500 with retries exhausted → ProductInfo with error (no BlockEvent)."""
    scraper = GenericScraper()

    async def _fast_fail(url: str, client: httpx.AsyncClient) -> str:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

    monkeypatch.setattr(generic_module, "_fetch_generic_html", _fast_fail)

    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/rate").respond(500)
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


# ── chain strategy fallback ───────────────────────────────────────


@pytest.mark.asyncio
async def test_jsonld_strategy_first(load_fixture: Callable[[str], str]) -> None:
    """JSON-LD wins when present: price>0 and currency populated."""
    html = load_fixture("generic/sample_jsonld.html")
    scraper = GenericScraper()
    url = "https://example.com/chain/jsonld"

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency != ""
    assert info.currency is not None


@pytest.mark.asyncio
async def test_falls_back_to_microdata(load_fixture: Callable[[str], str]) -> None:
    """Without JSON-LD, microdata yields a positive price."""
    html = load_fixture("generic/sample_microdata.html")
    scraper = GenericScraper()
    url = "https://example.com/chain/microdata"

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is not None
    assert info.price > Decimal("0")


@pytest.mark.asyncio
async def test_falls_back_to_opengraph(load_fixture: Callable[[str], str]) -> None:
    """Without JSON-LD/microdata, OpenGraph meta yields a positive price."""
    html = load_fixture("generic/sample_opengraph.html")
    scraper = GenericScraper()
    url = "https://example.com/chain/og"

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency == "USD"


@pytest.mark.asyncio
async def test_falls_back_to_rdfa(load_fixture: Callable[[str], str]) -> None:
    """Without JSON-LD/microdata/OG, RDFa Product+Offer yields a positive price."""
    html = load_fixture("generic/sample_rdfa.html")
    scraper = GenericScraper()
    url = "https://example.com/chain/rdfa"

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency == "EUR"


@pytest.mark.asyncio
async def test_heuristic_last_resort(
    load_fixture: Callable[[str], str], caplog: pytest.LogCaptureFixture
) -> None:
    """Regex on visible text recovers a price AND emits a heuristic-fallback warning."""
    caplog.set_level(logging.WARNING, logger="price_tracker.scrapers.generic")
    html = load_fixture("generic/sample_heuristic.html")
    scraper = GenericScraper()
    url = "https://example.com/chain/heuristic"

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is not None
    assert info.price > Decimal("0")
    heuristic_records = [r for r in caplog.records if "heuristic" in r.message.lower()]
    assert len(heuristic_records) == 1


@pytest.mark.asyncio
async def test_no_strategy_matches_returns_error() -> None:
    """When no strategy matches, ProductInfo carries an error and no price."""
    scraper = GenericScraper()
    html_no_markup = """
    <!DOCTYPE html><html><head><title>Bare Page</title></head>
    <body><p>This page has no structured data and no price text.</p></body></html>
    """
    url = "https://example.com/chain/none"

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html_no_markup)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None
    assert info.error is not None
