"""Unit tests for Nove25Scraper (JSON-LD, OpenGraph, microdata, CSS fallback)."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.nove25 import Nove25Scraper

if TYPE_CHECKING:
    from collections.abc import Callable


# ── can_handle ────────────────────────────────────────────────────


def test_nove25_can_handle_domain() -> None:
    scraper = Nove25Scraper()
    assert scraper.can_handle("https://www.nove25.net/it/bracciali/x")
    assert scraper.can_handle("https://nove25.net/it/anelli/y")


def test_nove25_rejects_other_domains() -> None:
    scraper = Nove25Scraper()
    assert not scraper.can_handle("https://www.amazon.it/dp/B01")
    assert not scraper.can_handle("https://fakenove25.com/it/p/x")


def test_nove25_priority_above_generic() -> None:
    assert Nove25Scraper.priority > 0


# ── scrape: extraction strategies ────────────────────────────────


@pytest.mark.asyncio
async def test_nove25_parses_jsonld_first(load_fixture: Callable[[str], str]) -> None:
    """Full fixture has JSON-LD + OG + microdata — JSON-LD wins."""
    html = load_fixture("nove25/sample_product.html")
    url = "https://www.nove25.net/it/bracciali/bracciale-in-argento-coda-di-volpe"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("65.00")
    assert info.currency == "EUR"
    assert info.name == "BRACCIALE IN ARGENTO CODA DI VOLPE"
    assert info.error is None


@pytest.mark.asyncio
async def test_nove25_falls_back_to_opengraph(load_fixture: Callable[[str], str]) -> None:
    """When JSON-LD absent, OpenGraph product:price:amount is used."""
    html = load_fixture("nove25/sample_no_jsonld.html")
    url = "https://www.nove25.net/it/anelli/anello-argento-minimal"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("42.50")
    assert info.currency == "EUR"
    assert info.error is None


@pytest.mark.asyncio
async def test_nove25_falls_back_to_microdata(load_fixture: Callable[[str], str]) -> None:
    """When JSON-LD and OpenGraph absent, microdata itemprop=price is used."""
    html = load_fixture("nove25/sample_itemprop_only.html")
    url = "https://www.nove25.net/it/collane/collana-eternity"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("119.90")
    assert info.currency == "EUR"
    assert info.error is None


@pytest.mark.asyncio
async def test_nove25_returns_error_when_no_price() -> None:
    """Page with no extractable price metadata yields error=Prezzo non trovato."""
    html = "<html><head><title>nada</title></head><body>no price here</body></html>"
    url = "https://www.nove25.net/it/x/y"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None
    assert info.error is not None
    assert "Nove25" in info.error


@pytest.mark.asyncio
async def test_nove25_handles_http_error() -> None:
    """HTTP error → ProductInfo with `error` set, never raises."""
    url = "https://www.nove25.net/it/dead/page"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(500)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None
    assert info.error is not None
