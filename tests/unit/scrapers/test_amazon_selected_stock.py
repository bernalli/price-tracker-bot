"""Positive and negative stock evidence belongs to the selected concrete offer."""

import json
from decimal import Decimal

import pytest

from tests.unit.scrapers.test_amazon_positive_stock import (
    ASIN,
    CART,
    PRICE,
    URL,
    accordion_page,
    availability,
    scrape,
)


@pytest.mark.parametrize("selected", ["", availability("In stock") + CART])
async def test_other_offer_sold_out_does_not_attest_the_selected_price(
    monkeypatch: pytest.MonkeyPatch, selected: str
) -> None:
    info = await scrape(
        monkeypatch, accordion_page(selected, availability("Currently unavailable"))
    )
    assert info.price == Decimal("29.99")
    assert info.available is True
    assert info.stock_evidence is bool(selected)


async def test_foreign_product_sold_out_does_not_veto_selected_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = '<div data-asin="B0FOREIGN00">' + availability("Currently unavailable") + "</div>"
    info = await scrape(monkeypatch, accordion_page(availability("In stock") + CART + foreign))
    assert info.available is True
    assert info.stock_evidence is True


@pytest.mark.parametrize("selected_stock", ["", "Currently unavailable"])
async def test_classic_stock_comes_from_the_buybox_containing_the_price(
    monkeypatch: pytest.MonkeyPatch, selected_stock: str
) -> None:
    page = (
        f'<link rel="canonical" href="{URL}"><h1 id="productTitle">Synthetic item</h1>'
        '<div id="desktop_buybox">' + availability("In stock") + CART + "</div>"
        '<div id="buyBoxInner"><div id="corePrice_feature_div">'
        + PRICE
        + "</div>"
        + (availability(selected_stock) if selected_stock else "")
        + "</div>"
    )
    info = await scrape(monkeypatch, page)
    assert info.price == Decimal("29.99")
    assert info.available is (not selected_stock)
    assert info.stock_evidence is bool(selected_stock)


async def test_unbound_price_cannot_choose_between_disjoint_classic_buyboxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = (
        f'<link rel="canonical" href="{URL}"><h1 id="productTitle">Synthetic item</h1>'
        '<div id="corePrice_feature_div">' + PRICE + "</div>"
        '<div id="desktop_buybox">' + availability("In stock") + CART + "</div>"
        '<div id="buyBoxInner"></div>'
    )
    info = await scrape(monkeypatch, page)
    assert info.price == Decimal("29.99")
    assert info.stock_evidence is False


async def test_jsonld_selected_offer_stock_is_not_a_different_markup_offer_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "asin": ASIN,
        "url": URL,
        "offers": {
            "@type": "Offer",
            "price": "29.99",
            "priceCurrency": "EUR",
            "availability": "https://schema.org/InStock",
        },
    }
    page = (
        f'<link rel="canonical" href="{URL}"><h1 id="productTitle">Synthetic item</h1>'
        f'<script type="application/ld+json">{json.dumps(product)}</script>'
        '<div id="usedAccordionRow">' + availability("Currently unavailable") + "</div>"
    )
    info = await scrape(monkeypatch, page)
    assert info.available is True
    assert info.stock_evidence is True


async def test_standalone_no_price_sold_out_evidence_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = (
        f'<link rel="canonical" href="{URL}"><h1 id="productTitle">Synthetic item</h1>'
        + availability("Currently unavailable")
    )
    info = await scrape(monkeypatch, page)
    assert info.price is None
    assert info.available is False
    assert info.stock_evidence is True
