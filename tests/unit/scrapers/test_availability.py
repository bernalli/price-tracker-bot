"""Availability must reflect declared stock state (#33).

Shopify variants carry ``available``; JSON-LD offers carry schema.org
``availability``. Both were ignored: sold-out products were reported
available=True with a placeholder price.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

if TYPE_CHECKING:
    from collections.abc import Mapping

from price_tracker.core.scraper_base import ProductInfo, jsonld_offer_availability
from price_tracker.scrapers.aliexpress import AliexpressScraper
from price_tracker.scrapers.ebay import EbayScraper
from price_tracker.scrapers.nove25 import Nove25Scraper
from price_tracker.scrapers.shopify import ShopifyScraper

# ── jsonld_offer_availability helper ──────────────────────────────


def test_jsonld_availability_in_stock_url() -> None:
    assert jsonld_offer_availability({"availability": "https://schema.org/InStock"}) is True


def test_jsonld_availability_out_of_stock_url() -> None:
    assert jsonld_offer_availability({"availability": "https://schema.org/OutOfStock"}) is False


def test_jsonld_availability_bare_sold_out_case_insensitive() -> None:
    assert jsonld_offer_availability({"availability": "soldout"}) is False


def test_jsonld_availability_absent_or_unknown_is_none() -> None:
    assert jsonld_offer_availability({"price": "10.00"}) is None
    assert jsonld_offer_availability({"availability": "BackOrder"}) is None
    assert jsonld_offer_availability("nope") is None
    assert jsonld_offer_availability(None) is None


def test_jsonld_availability_any_in_stock_offer_wins() -> None:
    offers = [
        {"availability": "https://schema.org/OutOfStock"},
        {"availability": "https://schema.org/InStock"},
    ]
    assert jsonld_offer_availability(offers) is True


# ── Shopify JSON API variants (#33a) ──────────────────────────────


async def _scrape_shopify(json_payload: Mapping[str, object]) -> ProductInfo:
    scraper = ShopifyScraper()
    url = "https://shop.example.com/products/sample"
    json_url = "https://shop.example.com/products/sample.json"
    html = (
        '<html><head><meta property="og:price:currency" content="EUR"></head><body></body></html>'
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(json_url).respond(200, json=json_payload)
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            return await scraper.scrape(url, client)


@pytest.mark.asyncio
async def test_shopify_sold_out_placeholder_marks_unavailable() -> None:
    """All variants available=false → keep first priced one but available=False."""
    payload = {
        "product": {
            "title": "Sold Out Item",
            "variants": [
                {"id": 1, "price": "0.01", "available": False},
                {"id": 2, "price": "39.99", "available": False},
            ],
        }
    }
    info = await _scrape_shopify(payload)
    assert info.price == Decimal("0.01")
    assert info.available is False


@pytest.mark.asyncio
async def test_shopify_prefers_available_variant_price() -> None:
    """First variant sold out with placeholder → take the purchasable variant."""
    payload = {
        "product": {
            "title": "Partially Available",
            "variants": [
                {"id": 1, "price": "0.01", "available": False},
                {"id": 2, "price": "39.99", "available": True},
            ],
        }
    }
    info = await _scrape_shopify(payload)
    assert info.price == Decimal("39.99")
    assert info.available is True


@pytest.mark.asyncio
async def test_shopify_mixed_variant_without_available_key_is_purchasable() -> None:
    """One variant declares available=False, another omits the key entirely:
    the keyless variant must be treated as purchasable (absent = available),
    not skipped as sold-out because ``None`` is falsy (#33 hardening).
    """
    payload = {
        "product": {
            "title": "Mixed Shape",
            "variants": [
                {"id": 1, "price": "0.01", "available": False},
                {"id": 2, "price": "39.99"},
            ],
        }
    }
    info = await _scrape_shopify(payload)
    assert info.price == Decimal("39.99")
    assert info.available is True


@pytest.mark.asyncio
async def test_shopify_no_availability_key_keeps_default_true() -> None:
    """Variants without 'available' key → previous behavior, available=True."""
    payload = {
        "product": {
            "title": "Legacy Shape",
            "variants": [{"id": 1, "price": "29.99"}],
        }
    }
    info = await _scrape_shopify(payload)
    assert info.price == Decimal("29.99")
    assert info.available is True


# ── JSON-LD availability wiring (ebay / nove25 / aliexpress, #33c) ─


def _jsonld_html(availability: str | None, price: str = "120.00") -> str:
    avail_attr = f', "availability": "{availability}"' if availability else ""
    return (
        '<html><head><script type="application/ld+json">'
        f'{{"@type": "Product", "name": "Thing", "offers":'
        f' {{"@type": "Offer", "price": "{price}", "priceCurrency": "EUR"{avail_attr}}}}}'
        "</script></head><body></body></html>"
    )


@pytest.mark.asyncio
async def test_ebay_jsonld_out_of_stock_marks_unavailable() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.ebay.it/itm/123").respond(
            200, text=_jsonld_html("https://schema.org/OutOfStock")
        )
        async with httpx.AsyncClient() as client:
            info = await EbayScraper().scrape("https://www.ebay.it/itm/123", client)
    assert info.price == Decimal("120.00")
    assert info.available is False


@pytest.mark.asyncio
async def test_ebay_jsonld_without_availability_keeps_default_true() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.ebay.it/itm/124").respond(200, text=_jsonld_html(None))
        async with httpx.AsyncClient() as client:
            info = await EbayScraper().scrape("https://www.ebay.it/itm/124", client)
    assert info.price == Decimal("120.00")
    assert info.available is True


@pytest.mark.asyncio
async def test_nove25_jsonld_out_of_stock_marks_unavailable() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://nove25.net/products/ring").respond(
            200, text=_jsonld_html("https://schema.org/OutOfStock")
        )
        async with httpx.AsyncClient() as client:
            info = await Nove25Scraper().scrape("https://nove25.net/products/ring", client)
    assert info.price == Decimal("120.00")
    assert info.available is False


@pytest.mark.asyncio
async def test_nove25_jsonld_without_availability_keeps_default_true() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://nove25.net/products/ring2").respond(200, text=_jsonld_html(None))
        async with httpx.AsyncClient() as client:
            info = await Nove25Scraper().scrape("https://nove25.net/products/ring2", client)
    assert info.price == Decimal("120.00")
    assert info.available is True


@pytest.mark.asyncio
async def test_aliexpress_jsonld_out_of_stock_marks_unavailable() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://it.aliexpress.com/item/100500.html").respond(
            200, text=_jsonld_html("https://schema.org/SoldOut", price="15.99")
        )
        async with httpx.AsyncClient() as client:
            info = await AliexpressScraper().scrape(
                "https://it.aliexpress.com/item/100500.html", client
            )
    assert info.price == Decimal("15.99")
    assert info.available is False


@pytest.mark.asyncio
async def test_aliexpress_jsonld_without_availability_keeps_default_true() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://it.aliexpress.com/item/100501.html").respond(
            200, text=_jsonld_html(None, price="15.99")
        )
        async with httpx.AsyncClient() as client:
            info = await AliexpressScraper().scrape(
                "https://it.aliexpress.com/item/100501.html", client
            )
    assert info.price == Decimal("15.99")
    assert info.available is True
