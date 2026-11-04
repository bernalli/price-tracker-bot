"""Complete, identity-bound theme data is authoritative over page heuristics."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

from price_tracker.scrapers.shopify import ShopifyScraper

_URL = "https://store.com/products/widget"
_VARIANTS = [{"id": 1, "price": 1000}, {"id": 2, "price": 2000}]
_PRODUCT = {
    "handle": "widget",
    "id": 10,
    "title": "Widget",
    "variants": _VARIANTS,
}


def _page(product: object) -> str:
    return f"<script>var meta = {json.dumps({'product': product})};</script>"


def _price(html: str, url: str = _URL) -> Decimal | None:
    info = ShopifyScraper()._read_product_page(html, url)
    return None if info is None else info.price


@given(variants=st.permutations(_VARIANTS))
def test_requested_variant_is_selected_independently_of_order(variants) -> None:
    assert _price(_page({**_PRODUCT, "variants": variants}), f"{_URL}?variant=2") == Decimal(20)


async def test_json_429_selects_requested_embedded_variant() -> None:
    url = f"{_URL}?variant=2"
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{_URL}.json").respond(429)
        router.get(url).respond(200, text=_page(_PRODUCT))
        async with httpx.AsyncClient() as client:
            info = await ShopifyScraper().scrape(url, client)
    assert info.price == Decimal(20)


@pytest.mark.parametrize("selector", ["", "?variant=1", "?variant=2", "?variant=3"])
@given(prices=st.lists(st.integers(100, 99999), min_size=2, max_size=4))
def test_duplicate_variant_identity_is_rejected(selector: str, prices: list[int]) -> None:
    variants = [{"id": 2, "price": price} for price in prices]
    html = _page({**_PRODUCT, "variants": variants})
    assert _price(html, _URL + selector) is None


@given(order=st.permutations([_PRODUCT, {**_PRODUCT, "title": "Other"}]))
def test_duplicate_product_payloads_are_rejected_in_any_order(order) -> None:
    assert _price("".join(_page(p) for p in order), f"{_URL}?variant=2") is None


@given(order=st.permutations([_PRODUCT, {**_PRODUCT, "handle": "other", "id": 20}]))
def test_foreign_product_does_not_win_by_document_order(order) -> None:
    assert _price("".join(_page(p) for p in order), f"{_URL}?variant=2") == Decimal(20)


@pytest.mark.parametrize(
    "product",
    [
        {**_PRODUCT, "handle": "other"},
        {"variants": [{"id": 2, "price": 2000}]},
        {**_PRODUCT, "url": "/products/other"},
        {**_PRODUCT, "variants": [{"price": 2000}]},
        {**_PRODUCT, "variants": [{"id": True, "price": 2000}]},
        {**_PRODUCT, "variants": [{"id": 2, "price": 199.99}]},
        {**_PRODUCT, "variants": [{"id": 1, "price": 2000}]},
        {**_PRODUCT, "variants": {"id": 2, "price": 2000}},
    ],
)
def test_rejected_source_cannot_be_rescued_by_page_heuristics(product: object) -> None:
    html = _page(product) + '<script>{"price_min":2999}</script><b class="product__price">49</b>'
    assert _price(html, f"{_URL}?variant=2") is None


@pytest.mark.parametrize("selector", ["?variant=2&variant=1", "?variant=", "?variant=2&variant=2"])
def test_ambiguous_variant_query_is_rejected(selector: str) -> None:
    assert _price(_page(_PRODUCT), _URL + selector) is None


def test_unselected_variants_must_agree() -> None:
    assert _price(_page(_PRODUCT)) is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"product":{"handle":"widget","handle":"other","variants":[{"id":2,"price":2000}]}}',
        '{"product":{"handle":"widget","variants":[{"id":2,"price":2000,"price":3000}]}}',
        '{"product":{"handle":"widget","variants":[{"id":2,"price":2000}]},"product":{}}',
        '{"product":{"handle":"widget","variants":[{"id":2,"price":NaN}]}}',
    ],
)
def test_strict_json_rejection_is_terminal(raw: str) -> None:
    html = f'<script>var meta = {raw};</script><script>{{"price_min":2999}}</script>'
    assert _price(html, f"{_URL}?variant=2") is None


_RAW = json.dumps({"product": _PRODUCT})


@given(cut=st.integers(min_value=1, max_value=len(_RAW) - 1))
@pytest.mark.parametrize("source", ["meta", "analytics", "script"])
def test_truncated_document_never_yields_a_price(cut: int, source: str) -> None:
    prefix = {
        "meta": "<script>var meta = ",
        "analytics": "<script>window.ShopifyAnalytics.meta = ",
        "script": '<script id="ProductJson-main" type="application/json">',
    }[source]
    assert _price(prefix + _RAW[:cut] + "</script>", f"{_URL}?variant=2") is None


def test_braces_and_semicolons_in_strings_do_not_truncate_payload() -> None:
    html = _page({**_PRODUCT, "title": 'Widget }; { " variants'})
    assert _price(html, f"{_URL}?variant=2") == Decimal(20)


def test_embedded_price_cannot_override_product_group_ambiguity() -> None:
    group = {
        "@type": "ProductGroup",
        "url": _URL,
        "hasVariant": [{"sku": "2", "offers": {"price": "10", "priceCurrency": "EUR"}}],
    }
    other = {
        **group,
        "hasVariant": [{"sku": "2", "offers": {"price": "30", "priceCurrency": "EUR"}}],
    }
    html = (
        _page(_PRODUCT)
        + f'<script type="application/ld+json">{json.dumps([group, other])}</script>'
    )
    assert _price(html, f"{_URL}?variant=2") is None


def test_unrelated_script_mentioning_product_cannot_reject_the_page() -> None:
    pixel = (
        "<script>window.webPixelsManager.init({"
        '"event": "product_viewed", "product": {"id": 99, "price": 5}});</script>'
    )
    html = pixel + _page(_PRODUCT)
    assert _price(html, f"{_URL}?variant=2") == Decimal(20)


@pytest.mark.parametrize(
    ("url", "handle"),
    [
        ("https://store.com/products/caf%C3%A9", "café"),
        ("https://store.com/products/Widget", "widget"),
    ],
)
def test_encoded_or_cased_handle_still_owns_the_page(url: str, handle: str) -> None:
    html = _page({**_PRODUCT, "handle": handle})
    assert _price(html, f"{url}?variant=2") == Decimal(20)
