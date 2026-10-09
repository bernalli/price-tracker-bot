"""Fallback formatting preserves readable prices, changes and availability."""

from decimal import Decimal

import pytest

from price_tracker.bot.ui.product_rows import product_row, record_row
from price_tracker.bot.ui.width import display_width
from tests.ui.test_narrow_lists import plain


@pytest.mark.parametrize("html", [False, True])
def test_product_rows_unknown_currency_falls_back_to_decimal(html):
    row = product_row("Widget <&>", Decimal("12.34"), currency="INVALID", html=html)
    assert (plain(row) if html else row) == "12.34 Widget <&>"
    if html:
        assert row == "<b>12.34</b> Widget &lt;&amp;&gt;"


def test_product_rows_percentage_over_formatter_limit_uses_scientific_change():
    row = product_row("Widget", Decimal("100"), initial=Decimal("0.00000000000001"))
    assert plain(row) == "€100.00 ▲1.0E+18% Widget"
    assert display_width(plain(row)) <= 32


@pytest.mark.parametrize("html", [False, True])
def test_product_rows_sold_out_record_has_availability_marker(html):
    row = record_row({"name": "Widget", "current_price": "12.34", "is_available": False}, html=html)
    assert (plain(row) if html else row) == "€12.34 🚫 Widget"
