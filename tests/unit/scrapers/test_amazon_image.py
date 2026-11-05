"""Amazon main photos require matching page echoes, price provenance and node context."""

import pytest

from tests.unit.scrapers.test_image_identity import AMAZON_URL, IMAGE, OTHER, html_scrape, ld, owner


def html(image: str, *, before: str = "", after: str = "", echo: str = AMAZON_URL) -> str:
    return (
        f'<link rel="canonical" href="{echo}"><h1 id="productTitle">Kettle</h1>'
        '<div id="corePrice_feature_div"><span class="a-price">'
        f'<span class="a-offscreen">EUR 19.00</span></span></div>{before}{image}{after}'
    )


@pytest.mark.parametrize("attribute", ["data-old-hires", "src"])
async def test_a_main_photo_matches_the_requested_asin(attribute: str) -> None:
    result = await html_scrape(
        "amazon", html(f'<img id="landingImage" {attribute}="{IMAGE}">'), url=AMAZON_URL
    )
    assert result.image_url == IMAGE


@pytest.mark.parametrize(
    "context",
    [
        '<div data-asin="B087654321">',
        '<div id="sims-recommendations">',
        '<div class="a-carousel">',
    ],
)
async def test_foreign_or_recommendation_photos_are_refused(context: str) -> None:
    result = await html_scrape(
        "amazon",
        html(f'<img id="landingImage" src="{OTHER}">', before=context, after="</div>"),
        url=AMAZON_URL,
    )
    assert result.image_url is None


async def test_missing_page_identity_is_not_replaced_by_an_absent_data_asin() -> None:
    page = html(f'<img id="landingImage" src="{IMAGE}">').replace(
        f'<link rel="canonical" href="{AMAZON_URL}">', ""
    )
    assert (await html_scrape("amazon", page, url=AMAZON_URL)).image_url is None


async def test_a_foreign_canonical_prevents_main_image_attribution() -> None:
    assert (
        await html_scrape(
            "amazon",
            html(
                f'<img id="landingImage" src="{IMAGE}">',
                echo="https://www.amazon.com/dp/B087654321",
            ),
            url=AMAZON_URL,
        )
    ).image_url is None


async def test_a_data_image_can_fall_back_to_attributed_page_metadata() -> None:
    page = (
        html('<img id="landingImage" src="data:image/png;base64,AAAA">')
        + f'<meta property="og:image" content="{IMAGE}">'
    )
    assert (await html_scrape("amazon", page, url=AMAZON_URL)).image_url == IMAGE


async def test_a_jsonld_price_override_invalidates_the_css_photo_binding() -> None:
    page = html(f'<img id="landingImage" src="{OTHER}">') + ld(
        owner(IMAGE, url=AMAZON_URL, offers={"price": "100.00", "priceCurrency": "EUR"})
    )
    result = await html_scrape("amazon", page, url=AMAZON_URL)
    assert str(result.price) == "100.00"
    assert result.image_url == IMAGE


@pytest.mark.parametrize(
    "attribute",
    [
        "",
        'src="data:image/png;base64,AAAA"',
        'src="javascript:bad"',
        'src="https://cdn.example.com/other.jpg"',
    ],
)
async def test_image_metadata_changes_no_other_amazon_field(attribute: str) -> None:
    from dataclasses import asdict

    left = asdict(
        await html_scrape("amazon", html(f'<img id="landingImage" src="{IMAGE}">'), url=AMAZON_URL)
    )
    right = asdict(
        await html_scrape("amazon", html(f'<img id="landingImage" {attribute}>'), url=AMAZON_URL)
    )
    left.pop("image_url")
    right.pop("image_url")
    assert left == right


async def test_a_new_price_override_replaces_the_jsonld_image_binding() -> None:
    page = html(f'<img id="landingImage" src="{OTHER}">') + ld(
        owner(IMAGE, url=AMAZON_URL, offers={"price": "100.00", "priceCurrency": "EUR"})
    )
    page = page.replace('<div id="corePrice_feature_div">', '<div id="corePrice_feature_div">Used')
    page += (
        '<div id="newAccordionRow"><span class="a-price">'
        '<span class="a-offscreen">EUR 25.00</span></span></div>'
    )
    result = await html_scrape("amazon", page, url=AMAZON_URL)
    assert str(result.price) == "25.00"
    assert result.image_url == OTHER
