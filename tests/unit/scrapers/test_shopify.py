"""Unit tests for ShopifyScraper (price parsing, error handling, can_handle)."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.core.exceptions import BlockEvent, ListingGone
from price_tracker.scrapers import shopify as shopify_module
from price_tracker.scrapers.shopify import ShopifyScraper

if TYPE_CHECKING:
    from collections.abc import Callable


# ── can_handle ────────────────────────────────────────────────────


def test_shopify_can_handle_known_domains():
    scraper = ShopifyScraper()
    assert scraper.can_handle("https://allbirds.com/products/runner")
    assert scraper.can_handle("https://gymshark.com/products/leggings")


def test_shopify_can_handle_products_path():
    scraper = ShopifyScraper()
    # Generic /products/<handle> on unknown domain → still True
    assert scraper.can_handle("https://shop.example.com/products/sample")
    assert scraper.can_handle("https://www.someshopify.io/products/widget-pro")


def test_shopify_rejects_amazon_ebay():
    scraper = ShopifyScraper()
    # Amazon URLs without /products/ should NOT match
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")
    assert not scraper.can_handle("https://www.ebay.com/itm/123")


def test_shopify_rejects_url_without_products_segment():
    scraper = ShopifyScraper()
    assert not scraper.can_handle("https://shop.example.com/collections/all")
    assert not scraper.can_handle("https://shop.example.com/")


def test_shopify_priority():
    assert ShopifyScraper.priority == 80


# ── scrape: happy path (HTML extraction) ─────────────────────────


@pytest.mark.asyncio
async def test_shopify_parses_fixture_html(load_fixture: Callable[[str], str]) -> None:
    """Fixture HTML has og:price + JSON-LD; JSON API returns 404 → falls back to HTML."""
    html = load_fixture("shopify/sample_product.html")
    scraper = ShopifyScraper()

    url = "https://shop.example.com/products/sample"
    json_url = "https://shop.example.com/products/sample.json"

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(404)
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("29.99")
    # Shopify HTML-fallback path doesn't set currency (only JSON-API path does);
    # current behavior returns None. Tracked as scraper limitation.
    assert info.currency in (None, "EUR")
    assert info.error is None


@pytest.mark.asyncio
async def test_shopify_parses_via_json_api() -> None:
    """If /products/<handle>.json returns Shopify product JSON, that path wins."""
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/sample"
    json_url = "https://shop.example.com/products/sample.json"
    json_payload = {
        "product": {
            "title": "Sample Product",
            "variants": [{"id": 1, "price": "29.99"}],
        }
    }
    # Minimal HTML used downstream for currency detection
    html = (
        '<html><head><meta property="og:price:currency" content="EUR"></head><body></body></html>'
    )

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(200, json=json_payload)
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("29.99")
    assert info.name == "Sample Product"
    # Currency should be detected via og:price:currency from HTML
    assert info.currency == "EUR"


@pytest.mark.asyncio
async def test_shopify_block_on_currency_html_does_not_discard_json_price() -> None:
    """When the JSON API already yielded a price, a BlockEvent raised by the
    currency-only HTML fetch must NOT discard the scrape.

    Root cause of the three Shopify stores quarantine: scrape() got the
    price from JSON, then fetched the HTML purely to detect currency; a block on
    that body propagated out of scrape() and poisoned an already-successful
    result, feeding the domain quarantine.
    """
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/sample"
    json_url = "https://shop.example.com/products/sample.json"
    json_payload = {"product": {"title": "Sample", "variants": [{"id": 1, "price": "199.00"}]}}
    # HTML page carries a genuine challenge marker → detect_block_event raises.
    blocked_html = '<html><body><form id="captcha-form">verify</form></body></html>'

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(200, json=json_payload)
        router.get(url).respond(200, text=blocked_html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("199.00")  # price survived the block
    assert info.name == "Sample"
    assert info.error is None
    # Currency may stay None (pre-existing HTML-detection limitation); the point
    # is the scrape did not crash and the price was preserved.
    assert info.currency in (None, "EUR")


# ── scrape: error paths ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_shopify_json_404_and_html_404_raises_listing_gone() -> None:
    """Both JSON API and HTML 404 identify a removed listing."""
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/missing"
    json_url = "https://shop.example.com/products/missing.json"

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(404)
        router.get(url).respond(404)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ListingGone) as exc_info:
                await scraper.scrape(url, client)

    assert exc_info.value.status == 404
    assert exc_info.value.url == url


@pytest.mark.asyncio
async def test_shopify_json_price_survives_html_404() -> None:
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/available"
    json_url = "https://shop.example.com/products/available.json"
    payload = {
        "product": {
            "title": "Available Product",
            "variants": [{"id": 1, "price": "29.99"}],
        }
    }

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(200, json=payload)
        router.get(url).respond(404)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price == Decimal("29.99")
    assert info.name == "Available Product"
    assert info.error is None


@pytest.mark.asyncio
async def test_shopify_json_404_and_html_410_raises_listing_gone() -> None:
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/removed"
    json_url = "https://shop.example.com/products/removed.json"

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(404)
        router.get(url).respond(410)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ListingGone) as exc_info:
                await scraper.scrape(url, client)

    assert exc_info.value.status == 410


@pytest.mark.asyncio
async def test_shopify_html_403_remains_block_event() -> None:
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/blocked"
    json_url = "https://shop.example.com/products/blocked.json"

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(404)
        router.get(url).respond(403, text="blocked")
        async with httpx.AsyncClient() as client:
            with pytest.raises(BlockEvent):
                await scraper.scrape(url, client)


@pytest.mark.asyncio
async def test_shopify_handles_server_error_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both paths return 5xx → a generic error, and no block is claimed.

    Deliberately a 500 and not a 429: a 403/429 is a *block* and must leave the
    scraper as a BlockEvent so the domain gets quarantined — that contract is
    covered in ``test_generic_shopify_block.py``. A 5xx is the site being
    broken, which is the per-product failure this test pins down.
    """
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/rate"
    json_url = "https://shop.example.com/products/rate.json"

    async def _fast_html(u: str, client: httpx.AsyncClient) -> httpx.Response:
        response = await client.get(u)
        response.raise_for_status()
        return response

    async def _fast_json(u: str, client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(u)

    monkeypatch.setattr(shopify_module, "_fetch_shopify_response", _fast_html)
    monkeypatch.setattr(shopify_module, "_fetch_shopify_json", _fast_json)

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(500)
        router.get(url).respond(500)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None
    assert info.error is not None


@pytest.mark.asyncio
async def test_shopify_missing_price_selectors() -> None:
    """HTML with no price markup → error, no crash."""
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/empty"
    json_url = "https://shop.example.com/products/empty.json"
    html_no_price = """
    <!DOCTYPE html><html><body>
    <h1 class="product__title">Stripped Item</h1>
    </body></html>
    """

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(404)
        router.get(url).respond(200, text=html_no_price)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None
    assert info.error is not None


# ── final-URL validation (regression: #21, #38) ──────────────────


@pytest.mark.asyncio
async def test_shopify_rejects_redirect_to_home() -> None:
    """Dead /products/<slug> that 301-redirects to home → must NOT parse price.

    Regression for bug that let `Filling Pieces® Official Webshop` get saved
    as a product name (og:title of the home page) when the original URL had
    been silently redirected. We reject any final URL whose path is not a
    /products/<slug>.
    """
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/dead-slug"
    json_url = "https://shop.example.com/products/dead-slug.json"
    home_html = """
    <!DOCTYPE html><html><head>
    <meta property="og:title" content="Shop Official Webshop">
    <script>var meta = {"product": {"variants": [{"price": "999.00"}]}};</script>
    </head><body>home page random price 999</body></html>
    """

    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(404)
        # Redirect product URL → home; respx follows redirects via httpx
        router.get(url).respond(302, headers={"Location": "https://shop.example.com/"})
        router.get("https://shop.example.com/").respond(200, text=home_html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None, "scraper must NOT parse price from home page"
    assert info.error is not None


@pytest.mark.asyncio
async def test_shopify_rejects_collection_url_with_known_domain() -> None:
    """Collection URL on a KNOWN_SHOPIFY_DOMAIN must not parse random price.

    Regression for bug that let `Men` (og:title of a collection page) get
    saved as a product when the URL was `/collections/men-new-arrivals?page=4`.
    """
    # Inject a known domain at the class level to take the KNOWN_SHOPIFY_DOMAINS
    # branch in can_handle (ClassVar — instance assignment is rejected by Pyright).
    ShopifyScraper.KNOWN_SHOPIFY_DOMAINS = {
        *ShopifyScraper.KNOWN_SHOPIFY_DOMAINS,
        "examplenshop.test",
    }
    scraper = ShopifyScraper()
    url = "https://examplenshop.test/collections/men-new-arrivals?page=4"
    collection_html = """
    <!DOCTYPE html><html><head>
    <meta property="og:title" content="Men">
    <script>var meta = {"product": {"variants": [{"price": "42.00"}]}};</script>
    </head><body>collection grid with random products</body></html>
    """

    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=collection_html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)

    assert info.price is None
    assert info.error is not None


def test_shopify_is_product_path_helper() -> None:
    """`_is_product_path` accepts /products/<slug>, rejects everything else."""
    from price_tracker.scrapers.shopify import _is_product_path

    assert _is_product_path("https://x.test/products/foo")
    assert _is_product_path("https://x.test/en-it/products/foo-bar_2")
    assert not _is_product_path("https://x.test/")
    assert not _is_product_path("https://x.test/collections/all")
    assert not _is_product_path("https://x.test/collections/men?page=4")
