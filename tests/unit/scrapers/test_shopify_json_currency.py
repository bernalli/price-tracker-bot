"""The Shopify ``.json`` variants carry the currency: no second request for it.

Every successful check used to fetch the HTML product page right after the
``.json`` call only to read the currency, doubling the requests a store sees
and pushing rate-limited stores into quarantine. The ``.json`` variants already
carry ``price_currency``; the HTML enrichment stays only for payloads without it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from price_tracker.scrapers.shopify import ShopifyScraper

if TYPE_CHECKING:
    from collections.abc import Callable

_URL = "https://shop.example.com/products/haywood-white-wood-ash"
_JSON_URL = f"{_URL}.json"
_EUR_PAGE = '<html><head><meta property="og:price:currency" content="EUR"></head></html>'
_USD_PAGE = '<html><head><meta property="og:price:currency" content="USD"></head></html>'


def _payload(*variants: dict[str, Any]) -> dict[str, Any]:
    return {
        "product": {
            "handle": "haywood-white-wood-ash",
            "title": "HAYWOOD",
            "variants": list(variants),
        }
    }


async def _scrape(payload: object, page: str = _USD_PAGE) -> tuple[Any, respx.Route, respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        json_route = router.get(_JSON_URL).respond(200, json=payload)
        html_route = router.get(_URL).respond(200, text=page)
        async with httpx.AsyncClient() as client:
            info = await ShopifyScraper().scrape(_URL, client)
    return info, json_route, html_route


async def test_real_payload_currency_comes_from_json_with_one_request(
    load_fixture: Callable[[str], str],
) -> None:
    payload = json.loads(load_fixture("shopify/haywood-white-wood-ash.json"))

    info, json_route, html_route = await _scrape(payload)

    assert info.price == Decimal("199.00")
    assert info.currency == "EUR"
    assert info.name == "HAYWOOD"
    assert info.error is None
    assert json_route.call_count == 1
    assert html_route.call_count == 0


async def test_currency_is_the_one_of_the_chosen_variant() -> None:
    """A sold-out first variant is skipped; the currency follows the price."""
    payload = _payload(
        {"price": "10.00", "price_currency": "GBP", "available": False},
        {"price": "25.00", "price_currency": "CHF", "available": True},
    )

    info, _, html_route = await _scrape(payload)

    assert info.price == Decimal("25.00")
    assert info.currency == "CHF"
    assert html_route.call_count == 0


async def test_currency_field_is_normalised() -> None:
    info, _, html_route = await _scrape(_payload({"price": "5.00", "price_currency": " eur "}))

    assert info.currency == "EUR"
    assert html_route.call_count == 0


async def test_payload_without_currency_keeps_html_enrichment() -> None:
    info, _, html_route = await _scrape(_payload({"price": "5.00"}), page=_EUR_PAGE)

    assert info.price == Decimal("5.00")
    assert info.currency == "EUR"
    assert html_route.call_count == 1


@pytest.mark.parametrize("bad_currency", ["", "EURO", "XYZ", 978, None, ["EUR"]])
async def test_unusable_currency_field_falls_back_to_html(bad_currency: object) -> None:
    """A field that is not an accepted ISO code is treated as absent, never trusted."""
    info, _, html_route = await _scrape(
        _payload({"price": "5.00", "price_currency": bad_currency}), page=_EUR_PAGE
    )

    assert info.price == Decimal("5.00")
    assert info.currency == "EUR"
    assert html_route.call_count == 1
