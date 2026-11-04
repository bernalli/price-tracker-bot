"""Theme-embedded Shopify product JSON carries prices as integer cents.

The ``.json`` API answers decimal strings (``"199.00"``), but the product JSON a
theme embeds in the page (``var meta = …``, ``ShopifyAnalytics.meta``, the
``ProductJson`` script) is rendered by Liquid's ``json`` filter, which writes
prices as integer cents (``19900``). Reading those as units made a 199.00 EUR
product 19900 EUR. Anything that is neither cents nor a decimal string yields
no price: a missing price is recoverable, a wrong one is pushed to the user.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

from price_tracker.scrapers.shopify import ShopifyScraper

# Trimmed from the real product page of a Shopify store (public catalogue data):
# the analytics ``meta`` block a theme renders into every product page.
_REAL_SHAPE_META = (
    "<script>window.ShopifyAnalytics = window.ShopifyAnalytics || {};"
    "window.ShopifyAnalytics.meta = window.ShopifyAnalytics.meta || {};"
    'window.ShopifyAnalytics.meta.currency = "EUR";'
    'var meta = {"product":{"id":15311073116535,'
    '"gid":"gid:\\/\\/shopify\\/Product\\/15311073116535",'
    '"vendor":"CLAE","type":"Sneakers","handle":"haywood-white-wood-ash","variants":['
    '{"id":56474066583927,"price":19900,"name":"HAYWOOD - 36 \\/ White Wood Ash",'
    '"public_title":"36 \\/ White Wood Ash","sku":"840385519977"},'
    '{"id":56474066616695,"price":19900,"name":"HAYWOOD - 37 \\/ White Wood Ash",'
    '"public_title":"37 \\/ White Wood Ash","sku":"840385519984"}],'
    '"remote":false},"page":{"pageType":"product","resourceType":"product",'
    '"resourceId":15311073116535}};'
    "for (var attr in meta) { window.ShopifyAnalytics.meta[attr] = meta[attr]; }</script>"
)


def _meta_page(price: object, *, title: str | None = "Widget") -> str:
    product: dict[str, object] = {"variants": [{"id": 1, "price": price}]}
    if title is not None:
        product["title"] = title
    return f"<script>var meta = {json.dumps({'product': product})};</script>"


def _read(html: str) -> Decimal | None:
    info = ShopifyScraper()._read_product_page(html, "https://store.com/products/widget")
    return None if info is None else info.price


def test_real_shape_meta_block_reads_cents() -> None:
    assert _read(_REAL_SHAPE_META) == Decimal("199.00")


def test_product_json_script_reads_cents() -> None:
    payload = {"title": "Widget", "variants": [{"id": 1, "price": 2999}]}
    html = f'<script type="application/json" id="ProductJson-main">{json.dumps(payload)}</script>'
    assert _read(html) == Decimal("29.99")


def test_analytics_meta_assignment_reads_cents() -> None:
    payload = {"product": {"variants": [{"id": 1, "price": 1234}]}}
    html = f"<script>window.ShopifyAnalytics.meta = {json.dumps(payload)};</script>"
    assert _read(html) == Decimal("12.34")


def test_decimal_string_price_stays_as_it_is() -> None:
    assert _read(_meta_page("199.00")) == Decimal("199.00")


@pytest.mark.parametrize(
    "price",
    [
        0,
        -19900,
        True,
        False,
        199.0,
        199.99,
        19900.0,
        10**15,
        None,
        [19900],
        {"amount": 19900},
    ],
    ids=[
        "zero",
        "negative",
        "true",
        "false",
        "float",
        "fractional-float",
        "float-cents",
        "huge",
        "null",
        "list",
        "obj",
    ],
)
def test_unusable_price_yields_no_price(price: object) -> None:
    assert _read(_meta_page(price)) is None


def test_missing_price_yields_no_price() -> None:
    html = '<script>var meta = {"product":{"title":"Widget","variants":[{"id":1}]}};</script>'
    assert _read(html) is None


def test_unusable_first_variant_falls_through_to_a_cents_one() -> None:
    payload = {"product": {"variants": [{"price": True}, {"price": 4500}]}}
    assert _read(f"<script>var meta = {json.dumps(payload)};</script>") == Decimal("45.00")


@given(cents=st.integers(min_value=-(10**13), max_value=10**13))
def test_any_integer_is_cents_or_nothing(cents: int) -> None:
    price = _read(_meta_page(cents))
    assert price is None or price == Decimal(cents) / 100
    if cents <= 0:
        assert price is None


@given(value=st.floats(allow_nan=True, allow_infinity=True))
def test_any_float_yields_no_price(value: float) -> None:
    # json.dumps writes NaN/Infinity tokens, which json.loads accepts.
    assert _read(_meta_page(value)) is None


@pytest.mark.asyncio
async def test_scrape_reads_cents_from_the_page_when_json_is_absent() -> None:
    url = "https://shop.example.com/products/haywood-white-wood-ash"
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{url}.json").respond(404)
        router.get(url).respond(200, text=f"<html><head>{_REAL_SHAPE_META}</head></html>")
        async with httpx.AsyncClient() as client:
            info = await ShopifyScraper().scrape(url, client)

    assert info.price == Decimal("199.00")
    assert info.error is None
