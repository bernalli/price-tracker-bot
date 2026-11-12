"""A buy-box price selected from the page markup can attest that the offer is in stock."""

from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from price_tracker.core.page_image import ImagePageHTML
from price_tracker.core.scraper_base import ProductInfo
from price_tracker.scrapers import amazon

ASIN = "B012345678"
URL = f"https://www.amazon.it/dp/{ASIN}"
PRICE = '<span class="a-price priceToPay"><span class="a-offscreen">29,99 €</span></span>'
CART = '<input id="add-to-cart-button" name="submit.add-to-cart" type="submit">'


def accordion_page(selected: str = "", other: str = "") -> str:
    """Two offers of the tracked product: the selected new one and a used one."""
    return (
        f'<link rel="canonical" href="{URL}">'
        '<div id="dp"><h1 id="productTitle">Synthetic item</h1>'
        '<div id="buybox"><div id="buyBoxAccordion">'
        f'<div id="newAccordionRow_0" data-asin="{ASIN}">'
        '<div id="apex_offerDisplay_desktop"><div id="corePrice_feature_div">'
        f"{PRICE}</div></div>{selected}</div>"
        f'<div id="usedAccordionRow" data-asin="{ASIN}">{other}</div>'
        "</div></div></div>"
    )


def single_offer_page(buybox: str = "", elsewhere: str = "") -> str:
    """The classic layout: price in the centre column, stock and buttons in the buy box."""
    return (
        f'<link rel="canonical" href="{URL}">'
        '<div id="dp"><h1 id="productTitle">Synthetic item</h1>'
        f'<div id="centerCol"><div id="corePrice_feature_div">{PRICE}</div></div>'
        f'<div id="rightCol"><div id="desktop_buybox">{buybox}</div></div>'
        f"{elsewhere}</div>"
    )


async def scrape(monkeypatch: pytest.MonkeyPatch, page: str) -> ProductInfo:
    html = ImagePageHTML(page, URL)
    monkeypatch.setattr(amazon, "_fetch_amazon_page", AsyncMock(return_value=html))
    async with httpx.AsyncClient() as client:
        return await amazon.AmazonScraper().scrape(URL, client)


def availability(text: str) -> str:
    return f'<div id="availability"><span>{text}</span></div>'


@pytest.mark.parametrize(
    "text",
    [
        "In stock",
        "Only 3 left in stock - order soon.",
        "Disponibilità immediata.",
        "Disponibilità: solo 2",
        "Auf Lager",
        "En stock",
        "Op voorraad",
    ],
)
async def test_stock_statement_of_the_selected_offer_is_evidence(monkeypatch, text):
    info = await scrape(monkeypatch, accordion_page(availability(text)))
    assert info.price == Decimal("29.99")
    assert info.currency == "EUR"
    assert info.available is True
    assert info.stock_evidence is True


async def test_purchase_button_of_the_selected_offer_is_evidence(monkeypatch):
    info = await scrape(monkeypatch, accordion_page(CART))
    assert info.price == Decimal("29.99")
    assert info.available is True
    assert info.stock_evidence is True


async def test_single_offer_layout_reads_stock_from_the_buy_box(monkeypatch):
    info = await scrape(monkeypatch, single_offer_page(availability("Disponibilità immediata.")))
    assert info.price == Decimal("29.99")
    assert info.available is True
    assert info.stock_evidence is True


async def test_single_offer_layout_accepts_the_purchase_button(monkeypatch):
    info = await scrape(monkeypatch, single_offer_page(CART))
    assert info.available is True
    assert info.stock_evidence is True


async def test_stock_of_another_offer_does_not_attest_the_selected_price(monkeypatch):
    info = await scrape(monkeypatch, accordion_page("", availability("In stock") + CART))
    assert info.price == Decimal("29.99")
    assert info.stock_evidence is False


async def test_no_statement_and_no_button_stays_unknown(monkeypatch):
    for page in (accordion_page(), single_offer_page()):
        info = await scrape(monkeypatch, page)
        assert info.price == Decimal("29.99")
        assert info.stock_evidence is False


async def test_stock_of_another_product_does_not_attest_the_selected_price(monkeypatch):
    card = f'<div data-asin="B0FOREIGN00">{availability("In stock")}{CART}</div>'
    info = await scrape(monkeypatch, single_offer_page("", card))
    assert info.price == Decimal("29.99")
    assert info.stock_evidence is False


@pytest.mark.parametrize("text", ["Non disponibile", "Currently unavailable.", "Nicht auf Lager"])
async def test_a_sold_out_statement_is_never_read_as_in_stock(monkeypatch, text):
    info = await scrape(monkeypatch, accordion_page(availability(text) + CART))
    assert not (info.stock_evidence and info.available)


async def test_price_outside_every_offer_row_stays_unknown_when_rows_exist(monkeypatch):
    page = (
        f'<link rel="canonical" href="{URL}">'
        '<div id="dp"><h1 id="productTitle">Synthetic item</h1>'
        f'<div id="centerCol"><div id="corePrice_feature_div">{PRICE}</div></div>'
        f'<div id="buyBoxAccordion"><div id="usedAccordionRow" data-asin="{ASIN}">'
        f"{availability('In stock')}{CART}</div></div></div>"
    )
    info = await scrape(monkeypatch, page)
    assert info.price == Decimal("29.99")
    assert info.stock_evidence is False


@pytest.mark.parametrize(
    "text",
    [
        "Disponibile presso questi venditori.",
        "Available from these sellers.",
        "Dostępny u tych sprzedawców.",
    ],
)
async def test_offers_from_other_sellers_are_not_the_selected_offer_in_stock(monkeypatch, text):
    """The statement points at other sellers' offers, whose price is not the one selected."""
    for page in (accordion_page(availability(text)), single_offer_page(availability(text))):
        info = await scrape(monkeypatch, page)
        assert info.price == Decimal("29.99")
        assert info.stock_evidence is False
