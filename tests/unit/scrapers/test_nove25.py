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


# ── _coerce_product (edge cases for JSON-LD shapes seen in the wild) ──


def test_coerce_product_handles_graph_wrapper() -> None:
    """JSON-LD payloads wrapped in `@graph` should resolve to the Product node."""
    payload = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "BreadcrumbList", "itemListElement": []},
            {"@type": "Product", "name": "X", "offers": {"price": "1"}},
        ],
    }
    result = Nove25Scraper._coerce_product(payload)
    assert isinstance(result, dict)
    assert result.get("name") == "X"


def test_coerce_product_handles_type_as_list() -> None:
    """`@type` can be a list (e.g. ["Product", "Thing"])."""
    payload = {"@type": ["Product", "Thing"], "offers": {"price": "1"}}
    assert Nove25Scraper._coerce_product(payload) is payload


def test_coerce_product_returns_none_for_non_product() -> None:
    """Non-Product nodes (Organization, Article…) yield None."""
    assert Nove25Scraper._coerce_product({"@type": "Organization"}) is None
    assert Nove25Scraper._coerce_product(None) is None
    assert Nove25Scraper._coerce_product("string") is None
    assert Nove25Scraper._coerce_product([]) is None


# ── JSON-LD malformed / partial branches ─────────────────────────


@pytest.mark.asyncio
async def test_nove25_skips_malformed_jsonld_then_uses_opengraph() -> None:
    """Malformed JSON-LD must not crash; scraper proceeds to OpenGraph fallback."""
    html = """
    <html><head>
      <script type="application/ld+json">{not-valid-json}</script>
      <meta property="product:price:amount" content="33.00">
      <meta property="product:price:currency" content="EUR">
    </head><body></body></html>
    """
    url = "https://www.nove25.net/it/x/malformed"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("33.00")
    assert info.currency == "EUR"


@pytest.mark.asyncio
async def test_nove25_jsonld_offers_as_list() -> None:
    """`offers` may be a list — first element wins."""
    html = """
    <html><head>
      <script type="application/ld+json">
      {"@type":"Product","name":"Listed","offers":[{"price":"12.34","priceCurrency":"EUR"}]}
      </script>
    </head></html>
    """
    url = "https://www.nove25.net/it/x/listed"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("12.34")
    assert info.name == "Listed"


@pytest.mark.asyncio
async def test_nove25_jsonld_missing_price_falls_through_to_css() -> None:
    """Product JSON-LD with no offers price → fall through to CSS `.product-price`."""
    html = """
    <html><head>
      <script type="application/ld+json">
      {"@type":"Product","name":"NoPrice","offers":{"sku":"X"}}
      </script>
    </head><body>
      <div class="product-price">€ 17,00</div>
    </body></html>
    """
    url = "https://www.nove25.net/it/x/css"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("17.00")
    # CSS path doesn't set a name → falls back to extract_name (no og:title, no h1)
    assert info.currency == "EUR"


@pytest.mark.asyncio
async def test_nove25_opengraph_unparsable_amount_falls_through() -> None:
    """og:price:amount with garbage content → OG path returns None, fall through."""
    html = """
    <html><head>
      <meta property="product:price:amount" content="not-a-number">
    </head><body>
      <div class="product-price">€ 9,99</div>
    </body></html>
    """
    url = "https://www.nove25.net/it/x/badog"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    # Fell through to CSS .product-price
    assert info.price == Decimal("9.99")


@pytest.mark.asyncio
async def test_nove25_microdata_unparsable_price_returns_error() -> None:
    """itemprop=price with non-numeric text and no other source → error."""
    html = """
    <html><body>
      <span itemprop="price">free</span>
    </body></html>
    """
    url = "https://www.nove25.net/it/x/badmicro"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_nove25_name_falls_back_to_h1() -> None:
    """When og:title absent but h1 present, h1 is used as product name."""
    html = """
    <html><head>
      <meta property="product:price:amount" content="50.00">
    </head><body><h1>FALLBACK NAME H1</h1></body></html>
    """
    url = "https://www.nove25.net/it/x/h1name"
    scraper = Nove25Scraper()

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("50.00")
    assert info.name == "FALLBACK NAME H1"
