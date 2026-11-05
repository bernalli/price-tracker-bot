"""Generic page images need the same positive identity evidence as their price."""

from typing import Any

import pytest

from tests.unit.scrapers.test_image_identity import IMAGE, OTHER, URL, html_scrape, ld, owner


async def test_the_attributed_jsonld_node_supplies_its_own_image() -> None:
    assert (await html_scrape("generic", ld(owner()))).image_url == IMAGE


@pytest.mark.parametrize("echo", ["", f'<meta property="og:url" content="{URL}">'])
async def test_opengraph_needs_positive_page_identity(echo: str) -> None:
    html = (
        echo + '<meta property="og:price:amount" content="19.00">'
        '<meta property="og:price:currency" content="EUR">'
        f'<meta property="og:image" content="{IMAGE}">'
    )
    assert (await html_scrape("generic", html)).image_url == (IMAGE if echo else None)


async def test_an_unattributed_selector_price_never_authorizes_opengraph() -> None:
    html = (
        f'<meta property="og:url" content="{URL}"><meta property="og:image" content="{OTHER}">'
        '<h1>Kettle</h1><div class="product-price">EUR 19.00</div>'
    )
    assert (await html_scrape("generic", html)).image_url is None


@pytest.mark.parametrize("image", [123, None, True, {}, {"url": []}, [False, 123]])
async def test_malformed_image_metadata_leaves_price_fields_unchanged(image: Any) -> None:
    result = await html_scrape("generic", ld(owner(image)))
    assert (
        result.name,
        str(result.price),
        result.currency,
        result.available,
        result.seller,
        result.condition,
        result.error,
    ) == ("Kettle", "19.00", "EUR", True, None, None, None)
    assert result.image_url is None
