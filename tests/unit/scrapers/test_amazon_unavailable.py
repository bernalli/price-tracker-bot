"""Amazon pages without a buy-box price must be classified, not counted as errors.

A delisted or retired product (reported 2026-11-01 on a LEGO Technic set) kept
failing with "price not found" until the 5/10 warning, because the scraper only
recognised a sold-out listing from ``#availability`` in Italian. These pages
must instead surface as sold out (``available=False``, restock alert later) or,
for Amazon's not-found page served with a 2xx status, as ``ListingGone``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.core.exceptions import ListingGone
from price_tracker.scrapers.amazon import AmazonScraper

if TYPE_CHECKING:
    from price_tracker.core.scraper_base import ProductInfo

pytestmark = pytest.mark.asyncio

_URL = "https://www.amazon.it/dp/B09QFSCWD9"


async def _scrape(html: str, monkeypatch: pytest.MonkeyPatch, url: str = _URL) -> ProductInfo:
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            return await AmazonScraper().scrape(url, client)


def _product_page(body: str) -> str:
    return (
        "<html><head><title>LEGO Technic : Amazon.it</title></head><body>"
        '<div id="dp"><h1 id="productTitle">LEGO Technic Ferrari Daytona SP3</h1>'
        f"{body}</div></body></html>"
    )


@pytest.mark.parametrize(
    "body",
    [
        # Modern layout: the sold-out notice lives in #outOfStock, not #availability.
        '<div id="outOfStock"><span class="a-color-price a-text-bold">Non disponibile.</span>'
        "<span>Non sappiamo se e quando l'articolo sarà di nuovo disponibile.</span></div>",
        # Classic layout.
        '<div id="availability"><span>Attualmente non disponibile.</span></div>',
        # Feature wrapper only.
        '<div id="availability_feature_div"><span>Non disponibile.</span></div>',
    ],
)
async def test_italian_sold_out_layouts_are_unavailable(
    body: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = await _scrape(_product_page(body), monkeypatch)
    assert info.available is False
    assert info.price is None


@pytest.mark.parametrize(
    ("url", "text"),
    [
        ("https://www.amazon.com/dp/B09QFSCWD9", "Currently unavailable."),
        ("https://www.amazon.de/dp/B09QFSCWD9", "Derzeit nicht verfügbar."),
        ("https://www.amazon.fr/dp/B09QFSCWD9", "Actuellement indisponible."),
        ("https://www.amazon.es/dp/B09QFSCWD9", "No disponible."),
    ],
)
async def test_other_locales_sold_out_is_unavailable(
    url: str, text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = _product_page(f'<div id="availability"><span>{text}</span></div>')
    info = await _scrape(html, monkeypatch, url=url)
    assert info.available is False


async def test_only_other_sellers_without_price_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _product_page(
        '<div id="availability"><span>Disponibile presso questi venditori.</span></div>'
        '<div id="buybox-see-all-buying-choices"><a href="/gp/offer-listing/B09QFSCWD9">'
        "Vedi tutte le offerte di acquisto</a></div>"
    )
    info = await _scrape(html, monkeypatch)
    assert info.available is False
    assert info.price is None


async def test_in_stock_page_stays_available(monkeypatch: pytest.MonkeyPatch) -> None:
    html = _product_page(
        '<div id="availability"><span>Disponibilità immediata.</span></div>'
        '<div id="corePrice_feature_div"><span class="a-price priceToPay">'
        '<span class="a-offscreen">473,97€</span></span></div>'
        '<input id="add-to-cart-button" type="submit">'
    )
    info = await _scrape(html, monkeypatch)
    assert info.available is True
    assert info.price is not None


async def test_missing_price_on_normal_page_stays_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A layout change (no price, no sold-out marker) must still count as an error."""
    html = _product_page('<div id="availability"><span>Disponibilità immediata.</span></div>')
    info = await _scrape(html, monkeypatch)
    assert info.available is True
    assert info.price is None
    assert info.error


async def test_not_found_page_with_200_raises_listing_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        "<html><head><title>Amazon.it Pagina non trovata</title></head><body>"
        '<a href="/ref=cs_404_logo"><img alt="Amazon.it"></a>'
        "<p>Ci dispiace. L'indirizzo web che hai inserito non è una pagina attiva "
        "del nostro sito.</p>"
        '<a href="/ref=cs_404_link"><img alt="I cani di Amazon"></a>'
        "</body></html>"
    )
    with pytest.raises(ListingGone) as exc:
        await _scrape(html, monkeypatch)
    assert exc.value.status == 404


async def test_server_error_page_is_not_listing_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Amazon's 503 dogs page (ref=cs_503_) is a transient error, not a removal."""
    html = (
        "<html><head><title>Amazon.it</title></head><body>"
        '<a href="/ref=cs_503_logo">Amazon</a><p>Ci dispiace! Si è verificato un errore.</p>'
        "</body></html>"
    )
    info = await _scrape(html, monkeypatch)
    assert info.price is None
    assert info.available is True
