"""An Amazon price must belong to the tracked product, never to a recommendation card.

Product pages carry recommendation, sponsored and carousel cards whose prices use
the same markup as the main price, each card inside an element whose `data-asin`
names the product it advertises. When the main buy box exposes no price, the
broad selectors used to pick the first card price and record it as the tracked
product's price.

Ownership rule: the nearest ancestor carrying `data-asin` decides. A price under
an owner that is not the tracked ASIN is rejected; when the tracked ASIN is
unknown, any owned price is rejected; a price with no owner at all is accepted.

All ASINs, names and prices here are invented.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from bs4 import BeautifulSoup

from price_tracker.scrapers import amazon as amazon_module
from price_tracker.scrapers.amazon import AmazonScraper, _extract_asin, _page_asin

TARGET = "B0TEST0001"
FOREIGN = "B0TEST0002"


def _soup(body: str) -> BeautifulSoup:
    return BeautifulSoup(f"<html><head></head><body>{body}</body></html>", "lxml")


def _card(asin: str, price: str) -> str:
    return (
        f'<div data-asin="{asin}"><span class="a-price">'
        f'<span class="a-offscreen">{price}</span></span></div>'
    )


# ── ASIN from the URL ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://www.example.com/dp/{TARGET}", TARGET),
        (f"https://www.example.com/Widget/dp/{TARGET}/ref=sr_1_1", TARGET),
        (f"https://www.example.com/gp/product/{TARGET.lower()}", TARGET),
        (f"https://www.example.com/gp/aw/d/{TARGET}?th=1", TARGET),
        (f"https://www.example.com/gp/offer-listing/{TARGET}/", TARGET),
        (f"https://www.example.com/exec/obidos/ASIN/{TARGET}", TARGET),
        (f"https://www.example.com/dp/product/{TARGET}", TARGET),
        (f"https://www.example.com/o/ASIN/{TARGET}", TARGET),
        (f"https://www.example.com/s?k={TARGET}", None),
        ("https://www.example.com/dp/B0TEST001", None),
        ("https://www.example.com/dp/B0TEST00011", None),
    ],
)
def test_extract_asin_reads_the_product_url_shapes(url: str, expected: str | None) -> None:
    assert _extract_asin(url) == expected


# ── ASIN from the page ────────────────────────────────────────────────


def test_page_asin_reads_the_canonical_link() -> None:
    soup = BeautifulSoup(
        f'<html><head><link rel="canonical" href="https://www.example.com/Widget/dp/{TARGET}">'
        "</head><body></body></html>",
        "lxml",
    )
    assert _page_asin(soup) == TARGET


def test_page_asin_reads_the_hidden_asin_input() -> None:
    soup = _soup(f'<form><input type="hidden" id="ASIN" name="ASIN" value="{TARGET}"></form>')
    assert _page_asin(soup) == TARGET


def test_page_asin_is_none_without_a_marker() -> None:
    assert _page_asin(_soup("<p>Widget</p>")) is None


# ── _extract_price ────────────────────────────────────────────────────


@pytest.mark.parametrize("target", [TARGET, None])
def test_a_foreign_card_price_is_rejected(target: str | None) -> None:
    soup = _soup(_card(FOREIGN, "218,75€"))
    assert AmazonScraper()._extract_price(soup, target_asin=target) is None


def test_a_price_owned_by_the_tracked_product_is_accepted() -> None:
    soup = _soup(_card(TARGET, "49,99€"))
    assert AmazonScraper()._extract_price(soup, target_asin=TARGET) == Decimal("49.99")


@pytest.mark.parametrize("target", [TARGET, None])
def test_an_unowned_price_is_accepted(target: str | None) -> None:
    soup = _soup('<span class="a-price"><span class="a-offscreen">49,99€</span></span>')
    assert AmazonScraper()._extract_price(soup, target_asin=target) == Decimal("49.99")


def test_the_nearest_owner_decides() -> None:
    soup = _soup(f'<div data-asin="{TARGET}">{_card(FOREIGN, "218,75€")}</div>')
    assert AmazonScraper()._extract_price(soup, target_asin=TARGET) is None


@pytest.mark.parametrize("target", [TARGET, None])
def test_an_empty_owner_is_not_the_tracked_product(target: str | None) -> None:
    soup = _soup(_card("", "218,75€"))
    assert AmazonScraper()._extract_price(soup, target_asin=target) is None


def test_a_foreign_card_before_the_real_price_is_skipped() -> None:
    soup = _soup(_card(FOREIGN, "218,75€") + _card(TARGET, "49,99€"))
    assert AmazonScraper()._extract_price(soup, target_asin=TARGET) == Decimal("49.99")


def test_a_foreign_card_inside_the_buy_box_is_rejected() -> None:
    soup = _soup(
        '<div id="corePrice_feature_div">'
        + _card(FOREIGN, "218,75€")
        + '<span class="a-price"><span class="a-offscreen">49,99€</span></span></div>'
    )
    assert AmazonScraper()._extract_price(soup, target_asin=TARGET) == Decimal("49.99")


def test_truncated_markup_with_only_a_foreign_card_yields_no_price() -> None:
    soup = BeautifulSoup(
        f'<div data-asin="{FOREIGN}"><span class="a-price"><span class="a-offscreen">218,75€',
        "lxml",
    )
    assert AmazonScraper()._extract_price(soup, target_asin=TARGET) is None


# ── _extract_new_price ────────────────────────────────────────────────


def test_new_price_from_a_foreign_card_is_rejected() -> None:
    soup = _soup(
        f'<div id="newAccordionRow_0" data-asin="{FOREIGN}">'
        '<span class="a-price"><span class="a-offscreen">218,75€</span></span></div>'
    )
    assert AmazonScraper()._extract_new_price(soup, target_asin=TARGET) is None


def test_new_price_owned_by_the_tracked_product_is_accepted() -> None:
    soup = _soup(
        '<div id="newAccordionRow_0">'
        '<span class="a-price"><span class="a-offscreen">59,99€</span></span></div>'
    )
    assert AmazonScraper()._extract_new_price(soup, target_asin=TARGET) == Decimal("59.99")


# ── scrape, end to end ────────────────────────────────────────────────


def _patch_page(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    async def fake_fetch(*_args: Any, **_kwargs: Any) -> str:
        return html

    monkeypatch.setattr(amazon_module, "_fetch_amazon_page", fake_fetch)


@pytest.mark.asyncio
async def test_scrape_with_an_unreadable_asin_does_not_take_a_card_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_page(
        monkeypatch,
        '<html><body><span id="productTitle">Widget</span>'
        + _card(FOREIGN, "218,75€")
        + "</body></html>",
    )

    info = await AmazonScraper().scrape("https://www.example.com/s?k=widget", None)  # type: ignore[arg-type]

    assert info.price is None


@pytest.mark.asyncio
async def test_scrape_uses_the_page_asin_when_the_url_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_page(
        monkeypatch,
        f'<html><head><link rel="canonical" href="https://www.example.com/dp/{TARGET}"></head>'
        '<body><span id="productTitle">Widget</span>'
        + _card(FOREIGN, "218,75€")
        + _card(TARGET, "49,99€")
        + "</body></html>",
    )

    info = await AmazonScraper().scrape("https://www.example.com/s?k=widget", None)  # type: ignore[arg-type]

    assert info.price == Decimal("49.99")
