"""Shopify images follow the exact priced variant and its verified associations."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx

from price_tracker.core.http_client import build_client
from price_tracker.scrapers import shopify
from tests.unit.scrapers.test_image_identity import (
    IMAGE,
    OTHER,
    URL,
    api_scrape,
    group,
    html_scrape,
    ld,
    owner,
)


def payload(**extra: Any) -> dict[str, Any]:
    return {
        "handle": "a",
        "title": "Kettle",
        "variants": [{"id": 1, "price": "19.00", "price_currency": "EUR", "image_id": None}],
        "image": {"src": IMAGE},
        **extra,
    }


async def test_all_null_variant_images_use_the_storefront_common_image() -> None:
    path = Path(__file__).parents[2] / "fixtures/shopify/haywood-white-wood-ash.json"
    product = json.loads(path.read_text())["product"]
    result = await api_scrape(product)
    assert result.image_url == product["image"]["src"]


@pytest.mark.parametrize("available", [True, False])
async def test_the_exact_priced_variant_owns_the_image_even_when_sold_out(available: bool) -> None:
    product = payload(
        variants=[
            {
                "id": 1,
                "price": "19.00",
                "available": available,
                "price_currency": "EUR",
                "image_id": 10,
            }
        ],
        images=[{"id": 10, "src": OTHER}],
    )
    result = await api_scrape(product)
    assert result.image_url == OTHER
    assert result.available is available


@pytest.mark.parametrize("image_id", [True, False, "bad", [], {}])
async def test_malformed_variant_image_ids_never_borrow_a_product_image(image_id: Any) -> None:
    product = payload(
        variants=[{"id": 1, "price": "19.00", "price_currency": "EUR", "image_id": image_id}]
    )
    assert (await api_scrape(product)).image_url is None


@pytest.mark.parametrize(
    "images", [[{"id": 10, "src": IMAGE}, {"id": 10, "src": OTHER}], [{"id": 11, "src": OTHER}], []]
)
async def test_variant_image_references_must_be_unique_and_resolvable(
    images: list[dict[str, Any]],
) -> None:
    product = payload(
        variants=[{"id": 1, "price": "19.00", "price_currency": "EUR", "image_id": 10}],
        images=images,
    )
    assert (await api_scrape(product)).image_url is None


async def test_a_variant_without_a_photo_does_not_borrow_a_sibling_photo() -> None:
    product = payload(
        variants=[
            {"id": 1, "price": "19.00", "price_currency": "EUR", "image_id": None},
            {"id": 2, "price": "19.00", "image_id": 10},
        ],
        images=[{"id": 10, "src": OTHER}],
    )
    assert (await api_scrape(product)).image_url is None


@pytest.mark.parametrize(("selector", "expected"), [("1", IMAGE), ("2", None)])
async def test_requested_and_priced_variants_must_agree(
    selector: str, expected: str | None
) -> None:
    assert (await api_scrape(payload(), selector)).image_url == expected


@pytest.mark.parametrize("image", [None, {"src": "javascript:bad"}, {"src": 123}, True])
async def test_missing_or_unsafe_images_stay_absent(image: Any) -> None:
    assert (await api_scrape(payload(image=image))).image_url is None


async def test_an_attributed_product_group_can_supply_an_image() -> None:
    result = await html_scrape("shopify", ld(group()))
    assert result.image_url == IMAGE
    assert str(result.price) == "19.00"


JSONLD_PRODUCT = owner(name="Kettle JSON", offers={"price": "25.00", "priceCurrency": "USD"})


@pytest.mark.parametrize("json_status", [404, 429])
@pytest.mark.parametrize(
    ("page", "expected"),
    [
        # CSS 19,00 with the page currency EUR beside a JSON-LD Product at 25.00 USD.
        (
            '<meta property="og:price:currency" content="EUR"><h1>Kettle CSS</h1>'
            '<div class="product-price">19,00 \u20ac</div>' + ld(JSONLD_PRODUCT),
            ("Kettle CSS", Decimal("19.00"), "EUR", None),
        ),
        # The JSON-LD Product only supplies the currency, as it always did.
        (
            '<h1>Kettle CSS</h1><span class="price"><span class="money">12.50</span></span>'
            + ld(JSONLD_PRODUCT),
            ("Kettle CSS", Decimal("12.50"), "USD", None),
        ),
        # A JSON-LD Product alone never decided a price on this path.
        (ld(JSONLD_PRODUCT), (None, None, None, "Prezzo non trovato (Shopify)")),
    ],
)
async def test_the_html_fallback_keeps_the_page_price_path(
    json_status: int, page: str, expected: tuple[Any, ...]
) -> None:
    html = page + "<!--" + " " * 90000 + "-->"
    with respx.mock(assert_all_called=False) as router:
        router.get(URL + ".json").respond(json_status)
        router.get(URL).respond(200, text=html)
        async with build_client() as client:
            result = await shopify.ShopifyScraper().scrape(URL, client)
    assert (result.name, result.price, result.currency, result.error) == expected
    assert result.image_url is None


async def test_product_group_consensus_uses_the_group_image_and_never_the_first_variant() -> None:
    group = {
        "@type": "ProductGroup",
        "url": URL,
        "name": "Kettle",
        "image": IMAGE,
        "hasVariant": [owner(OTHER, url=URL + "?variant=1"), owner(OTHER, url=URL + "?variant=2")],
    }
    assert (await html_scrape("shopify", ld(group))).image_url == IMAGE
    del group["image"]
    assert (await html_scrape("shopify", ld(group))).image_url is None


async def test_theme_embedded_json_never_supplies_an_image() -> None:
    html = (
        '<script id="ProductJson-main">'
        + json.dumps(
            {
                "id": 1,
                "handle": "a",
                "title": "Kettle",
                "image": IMAGE,
                "variants": [{"id": 1, "price": 1900}],
            }
        )
        + "</script>"
    )
    assert (await html_scrape("shopify", html)).image_url is None


async def test_explicit_variant_image_and_gallery_reference_must_agree() -> None:
    product = payload(
        variants=[
            {
                "id": 1,
                "price": "19.00",
                "price_currency": "EUR",
                "image_id": 10,
                "image": {"src": IMAGE},
            }
        ],
        images=[{"id": 10, "src": OTHER}],
    )
    assert (await api_scrape(product)).image_url is None


@pytest.mark.parametrize(
    "metadata", [None, {}, [], True, {"src": "javascript:bad"}, {"src": OTHER}]
)
async def test_api_image_metadata_changes_no_other_field(metadata: Any) -> None:
    from dataclasses import asdict

    left = asdict(await api_scrape(payload()))
    right = asdict(await api_scrape(payload(image=metadata)))
    left.pop("image_url")
    right.pop("image_url")
    assert left == right
