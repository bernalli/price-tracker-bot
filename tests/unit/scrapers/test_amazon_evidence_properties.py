"""Stock and identity evidence holds for malformed inputs and unknown layouts too."""

import json
from decimal import Decimal

import pytest

from tests.unit.scrapers.test_amazon_positive_stock import (
    ASIN,
    CART,
    PRICE,
    URL,
    availability,
    scrape,
    single_offer_page,
)

# A well-formed ASIN of another product: ten characters, unlike a malformed identifier.
OTHER_ASIN = "B0OTHER123"
HEAD = f'<link rel="canonical" href="{URL}"><div id="dp"><h1 id="productTitle">Synthetic item</h1>'
CENTRE_PRICE = f'<div id="centerCol"><div id="corePrice_feature_div">{PRICE}</div></div>'


def test_the_other_asin_is_well_formed() -> None:
    assert len(ASIN) == len(OTHER_ASIN) == 10
    assert OTHER_ASIN != ASIN


@pytest.mark.parametrize(
    "statement",
    [
        '<div id="rightCol"><div id="mobile_buybox">'
        + availability("Currently unavailable.")
        + "</div></div>",
        '<div id="usedOnlyBuybox">' + availability("Non disponibile") + "</div>",
        '<div id="outOfStock"><span>Currently unavailable.</span></div>',
    ],
    ids=["unknown-box", "used-only-box", "bare-out-of-stock"],
)
async def test_sold_out_with_a_price_survives_an_unknown_single_offer_layout(
    monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    info = await scrape(monkeypatch, HEAD + CENTRE_PRICE + statement + "</div>")
    assert info.price == Decimal("29.99")
    assert info.available is False
    assert info.stock_evidence is True


async def test_unknown_layout_still_ignores_another_product_and_other_sellers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = f'<div data-asin="{OTHER_ASIN}">' + availability("Currently unavailable.") + "</div>"
    sellers = '<div id="aod-offer">' + availability("Currently unavailable.") + "</div>"
    info = await scrape(monkeypatch, HEAD + CENTRE_PRICE + foreign + sellers + "</div>")
    assert info.price == Decimal("29.99")
    assert info.available is True
    assert info.stock_evidence is False


async def test_sold_out_in_the_outer_buy_box_binds_a_price_in_a_nested_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = (
        HEAD
        + '<div id="desktop_buybox">'
        + availability("Currently unavailable.")
        + f'<div id="buybox"><div id="buyBoxInner"><div id="corePrice_feature_div">{PRICE}</div>'
        + "</div></div></div></div>"
    )
    info = await scrape(monkeypatch, page)
    assert info.price == Decimal("29.99")
    assert info.available is False
    assert info.stock_evidence is True


@pytest.mark.parametrize(
    "control",
    [
        f'<span class="a-button a-button-disabled">{CART}</span>',
        f'<div class="aok-hidden">{CART}</div>',
        '<input name="submit.add-to-cart" type="hidden" value="1">',
        '<input id="add-to-cart-button" type="submit" aria-disabled="TRUE">',
        '<input id="add-to-cart-button" type="submit" disabled="false">',
    ],
    ids=["aui-disabled", "aok-hidden", "hidden-input", "aria-upper", "disabled-false"],
)
async def test_a_control_nobody_can_press_cannot_prove_stock(
    monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    info = await scrape(monkeypatch, single_offer_page(control))
    assert info.price == Decimal("29.99")
    assert info.stock_evidence is False


@pytest.mark.parametrize("used_stock", ["In stock", "Temporarily out of stock."])
async def test_new_price_outside_the_used_buy_box_borrows_no_stock_from_it(
    monkeypatch: pytest.MonkeyPatch, used_stock: str
) -> None:
    page = (
        HEAD
        + '<div id="centerCol"><div id="corePrice_feature_div"><span>Usato</span>'
        + PRICE
        + '</div></div><div id="desktop_buybox">'
        + availability(used_stock)
        + CART
        + '</div><div id="olp_feature_div"><span id="newBuyBoxPrice">49,99 €</span></div></div>'
    )
    info = await scrape(monkeypatch, page)
    assert info.price == Decimal("49.99")
    assert info.condition == "new"
    assert info.available is True
    assert info.stock_evidence is False


def product_page(offers: object) -> str:
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "asin": ASIN,
        "url": URL,
        "offers": offers,
    }
    return HEAD + f'<script type="application/ld+json">{json.dumps(product)}</script></div>'


def offer(**identity: object) -> dict[str, object]:
    return {
        "@type": "Offer",
        **identity,
        "price": "29.99",
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
    }


OTHER_URL = f"https://www.amazon.it/dp/{OTHER_ASIN}"


@pytest.mark.parametrize(
    "offers",
    [
        offer(asin=OTHER_ASIN),
        offer(sku=OTHER_ASIN),
        offer(productID=f"ASIN:{OTHER_ASIN}"),
        offer(url=OTHER_URL),
        offer(itemOffered={"@type": "Product", "asin": OTHER_ASIN}),
        offer(itemOffered={"@type": "Product", "sku": OTHER_ASIN}),
        offer(itemOffered={"@id": OTHER_URL}),
        offer(itemOffered=OTHER_URL),
        offer(itemOffered=[{"asin": ASIN}, {"asin": OTHER_ASIN}]),
        [offer(asin=ASIN), offer(asin=OTHER_ASIN)],
        [offer(asin=OTHER_ASIN), offer(asin=ASIN)],
        offer(asin=[ASIN, OTHER_ASIN]),
        offer(asin=None),
        offer(asin=1234567890),
        offer(asin=[]),
        offer(asin={"value": ASIN}),
        offer(itemOffered={"asin": None}),
        offer(itemOffered={"url": 7}),
    ],
    ids=[
        "offer-asin",
        "offer-sku",
        "offer-product-id",
        "offer-url",
        "item-asin",
        "item-sku",
        "item-id",
        "item-string-url",
        "item-list",
        "second-offer",
        "first-offer",
        "asin-list",
        "asin-null",
        "asin-number",
        "asin-empty-list",
        "asin-object",
        "item-asin-null",
        "item-url-number",
    ],
)
async def test_an_offer_naming_another_or_no_valid_product_is_never_the_price(
    monkeypatch: pytest.MonkeyPatch, offers: object
) -> None:
    info = await scrape(monkeypatch, product_page(offers))
    assert info.price is None
    assert info.stock_evidence is False


@pytest.mark.parametrize(
    "offers",
    [
        offer(sku="MERCHANT-SKU-1"),
        offer(sku=ASIN, productID=f"ASIN:{ASIN}"),
        offer(itemOffered=URL),
        offer(itemOffered="Synthetic item"),
        [offer(asin=ASIN), "not an offer", 3, None],
    ],
    ids=["merchant-sku", "own-sku", "own-string-url", "item-name", "junk-entries"],
)
async def test_an_offer_consistent_with_the_tracked_product_keeps_its_price(
    monkeypatch: pytest.MonkeyPatch, offers: object
) -> None:
    info = await scrape(monkeypatch, product_page(offers))
    assert info.price == Decimal("29.99")
    assert info.available is True
    assert info.stock_evidence is True
