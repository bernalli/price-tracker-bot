"""A positive stock observation must describe a purchasable tracked offer."""

from decimal import Decimal

import pytest

from tests.unit.scrapers.test_amazon_positive_stock import (
    accordion_page,
    scrape,
    single_offer_page,
)


@pytest.mark.parametrize(
    "control",
    [
        '<input id="add-to-cart-button" type="submit" disabled>',
        '<input id="buy-now-button" type="submit" aria-disabled="true">',
        '<fieldset disabled><input id="add-to-cart-button" type="submit"></fieldset>',
    ],
    ids=["disabled", "aria-disabled", "disabled-fieldset"],
)
async def test_unusable_purchase_control_cannot_prove_stock(
    monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    info = await scrape(monkeypatch, accordion_page(control))
    assert info.price == Decimal("29.99")
    assert info.stock_evidence is False


@pytest.mark.parametrize("inside_buybox", [False, True])
async def test_another_seller_control_cannot_prove_the_selected_price_stock(
    monkeypatch: pytest.MonkeyPatch, inside_buybox: bool
) -> None:
    other = '<div id="aod-offer"><input name="submit.add-to-cart" type="submit"></div>'
    page = single_offer_page(other if inside_buybox else "", "" if inside_buybox else other)
    info = await scrape(monkeypatch, page)
    assert info.price == Decimal("29.99")
    assert info.stock_evidence is False
