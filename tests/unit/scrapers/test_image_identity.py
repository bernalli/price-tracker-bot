"""Public scraper invariants for image ownership and price isolation."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest
import respx
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.http_client import build_client
from price_tracker.scrapers import amazon, generic, shopify

URL = "https://shop.example.com/products/a"
IMAGE = "https://cdn.example.com/a.jpg"
OTHER = "https://cdn.example.com/b.jpg"
AMAZON_URL = "https://www.amazon.com/dp/B012345678"


def owner(image: Any = IMAGE, **extra: Any) -> dict[str, Any]:
    return {
        "@type": "Product",
        "url": URL,
        "name": "Kettle",
        "image": image,
        "offers": {"price": "19.00", "priceCurrency": "EUR"},
        **extra,
    }


def ld(nodes: Any, prefix: str = "") -> str:
    return prefix + f'<script type="application/ld+json">{json.dumps(nodes)}</script>'


async def html_scrape(kind: str, html: str, *, url: str = URL, redirect: str | None = None):
    instances = {
        "generic": generic.GenericScraper(),
        "shopify": shopify.ShopifyScraper(),
        "amazon": amazon.AmazonScraper(),
    }
    html += "<!--" + " " * 90000 + "-->"
    with respx.mock(assert_all_called=False) as router:
        router.get(URL + ".json").respond(404)
        if redirect:
            router.get(url).respond(302, headers={"location": redirect})
            router.get(redirect).respond(200, text=html)
        else:
            router.get(url).respond(200, text=html)
        async with build_client() as client:
            return await instances[kind].scrape(url, client)


async def api_scrape(product: dict[str, Any], selector: str | None = None):
    with respx.mock(assert_all_called=False) as router:
        url = f"https://shop.example.com/products/{product.get('handle', 'a')}"
        router.get(url + ".json").respond(200, json={"product": product})
        router.get(url).respond(200, text='<meta property="og:price:currency" content="EUR">')
        async with build_client() as client:
            return await shopify.ShopifyScraper().scrape(
                url + (f"?variant={selector}" if selector else ""), client
            )


@settings(max_examples=20, deadline=None)
@given(st.permutations([0, 1, 2]))
async def test_public_generic_selection_is_invariant_under_graph_permutation(
    order: list[int],
) -> None:
    nodes = [
        owner(),
        owner(OTHER, url="https://shop.example.com/products/b"),
        {"@type": "ItemList", "itemListElement": [owner(OTHER)]},
    ]
    result = await html_scrape("generic", ld({"@graph": [nodes[i] for i in order]}))
    assert result.image_url == IMAGE
    assert str(result.price) == "19.00"


@pytest.mark.parametrize("kind", ["generic", "shopify"])
async def test_image_metadata_does_not_change_any_other_product_field(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = ld(owner())
    result = await html_scrape(kind, html)
    module = {"generic": generic, "shopify": shopify}[kind]
    monkeypatch.setattr(module, "extract_page_image", lambda *args, **kwargs: None)
    without = await html_scrape(kind, html)
    left, right = asdict(result), asdict(without)
    left.pop("image_url")
    right.pop("image_url")
    assert left == right


@pytest.mark.parametrize("kind", ["generic", "shopify"])
async def test_a_redirect_to_another_product_cannot_supply_an_image(kind: str) -> None:
    result = await html_scrape(kind, ld(owner()), redirect="https://shop.example.com/products/b")
    assert result.image_url is None


@pytest.mark.parametrize("reverse", [False, True])
async def test_public_conflicting_owner_images_are_terminal(reverse: bool) -> None:
    nodes = [owner(), owner(OTHER)]
    if reverse:
        nodes.reverse()
    result = await html_scrape(
        "generic", ld(nodes, f'<meta property="og:image" content="{IMAGE}">')
    )
    assert result.image_url is None
    assert str(result.price) == "19.00"


@pytest.mark.parametrize("kind", ["generic", "shopify"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_public_duplicates_and_recommendations_preserve_the_attributed_image(
    kind: str, reverse: bool
) -> None:
    nodes = [owner(), owner(), owner(OTHER, url="https://shop.example.com/products/b")]
    if reverse:
        nodes.reverse()
    result = await html_scrape(kind, ld({"@graph": nodes}))
    assert result.image_url == IMAGE


@pytest.mark.parametrize("reverse", [False, True])
async def test_public_shopify_variant_selection_survives_reordered_variants(reverse: bool) -> None:
    variants = [
        owner(IMAGE, url=URL + "?variant=1", sku="1"),
        owner(OTHER, url=URL + "?variant=2", sku="2"),
    ]
    if reverse:
        variants.reverse()
    group = {"@type": "ProductGroup", "url": URL, "name": "Kettle", "hasVariant": variants}
    result = await html_scrape("shopify", ld(group), url=URL + "?variant=1")
    assert result.image_url == IMAGE
    assert str(result.price) == "19.00"


@pytest.mark.parametrize("kind", ["generic", "shopify"])
async def test_a_requested_owner_without_an_image_never_borrows_a_recommendation(kind: str) -> None:
    result = await html_scrape(
        kind, ld([owner(None), owner(OTHER, url="https://shop.example.com/products/b")])
    )
    assert result.image_url is None


@pytest.mark.parametrize("kind", ["generic", "shopify"])
@pytest.mark.parametrize("type_value", [123, ["Product", 1], None, {}])
async def test_malformed_image_metadata_isolated_from_other_product_fields(
    kind: str, type_value: Any
) -> None:
    base = ld(owner(), '<h1>Kettle</h1><div class="product-price">EUR 19.00</div>')
    added = ld({"@type": type_value, "image": OTHER})
    left = asdict(await html_scrape(kind, base))
    right = asdict(await html_scrape(kind, base + added))
    left.pop("image_url")
    right.pop("image_url")
    assert left == right


@pytest.mark.parametrize("kind", ["generic", "shopify"])
@pytest.mark.parametrize("budget", ["scripts", "nodes", "candidates"])
@pytest.mark.parametrize("over", [False, True])
async def test_public_extraction_enforces_whole_document_budgets(
    kind: str, budget: str, over: bool
) -> None:
    if budget == "scripts":
        page = ld(owner()) + ld({}) * (9 + int(over))
    elif budget == "nodes":
        page = ld(owner(extra=[0] * (9991 + int(over))))
    else:
        page = ld(owner([IMAGE] * (20 + int(over))))
    result = await html_scrape(kind, page)
    assert result.image_url == (None if over else IMAGE)
