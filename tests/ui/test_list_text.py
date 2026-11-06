"""Numbered list rows stay narrow, safe and bound to the correct products."""

from __future__ import annotations

import re
from decimal import Decimal
from html import escape, unescape
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.app.views import ListPage
from price_tracker.bot.callbacks import Action, decode
from price_tracker.bot.handlers.callbacks._nav import _with_notice
from price_tracker.bot.messages import _, current_locale, set_locale
from price_tracker.bot.ui.list_text import list_text_page
from price_tracker.bot.ui.product_rows import ROW_WIDTH, _compact_change, amount, product_row
from price_tracker.bot.ui.width import display_width, sanitize_label, truncate_to_width
from price_tracker.core.textlimits import (
    TELEGRAM_MESSAGE_LIMIT,
    _is_valid_telegram_markup,
    visible_length,
)
from price_tracker.i18n.format import change, money
from tests.support.list_variants import view


def plain(text: str) -> str:
    return unescape(re.sub(r"<[^>]*>", "", text))


@pytest.mark.parametrize("locale", ["en", "it"])
def test_six_products_have_one_number_row_navigation_counts_and_footer(
    ui_locales: Any, locale: str
) -> None:
    set_locale(locale)
    items = tuple(view(n, f"Product {n}") for n in range(7, 13))
    page = ListPage(filter="a", page=2, pages=3, total=18, items=items, counts=(18, 2, 1, 0))
    screen = list_text_page(page)
    assert [button.label for button in screen.rows[0]] == ["1", "2", "3", "4", "5", "6"]
    assert [decode(button.callback) for button in screen.rows[0]] == [
        Action("list.open", ("a", 2, n)) for n in range(7, 13)
    ]
    assert [button.label for button in screen.rows[1]] == ["◀️", "2/3", "▶️"]
    assert [decode(button.callback) for button in screen.rows[1]] == [
        Action("list.page", ("a", 1)),
        Action("noop"),
        Action("list.page", ("a", 3)),
    ]
    assert [button.label for button in screen.rows[2]] == ["• ✅ 18 •", "⏸ 2", "⚠️ 1", "🚫 0"]
    assert [decode(button.callback) for button in screen.rows[3]] == [Action("list.remove_all")]
    assert [decode(button.callback) for button in screen.rows[4]] == [
        Action("add"),
        Action("home"),
    ]
    assert [button.label for button in screen.rows[4]] == [_("➕ Add"), _("🏠 Home")]
    lines = screen.text.splitlines()
    rows = [n for n, line in enumerate(lines) if re.match(r"^<b>\d\.</b> ", line)]
    assert len(rows) == 6
    for n, index in enumerate(rows, 1):
        assert lines[index].startswith(f"<b>{n}.</b> ")
        assert display_width(plain(lines[index])) <= 34
        assert f"Product {n + 6}" in lines[index]
        assert lines[index + 1].startswith("    <b>")
    assert screen.disable_link_preview


@pytest.mark.parametrize("locale", ["en", "it"])
def test_paused_empty_pages_and_large_filter_counts(ui_locales: Any, locale: str) -> None:
    set_locale(locale)
    page = ListPage(
        filter="p", page=1, pages=1, total=0, items=(), counts=(100000, 0, 100000, 100000)
    )
    screen = list_text_page(page)
    assert _("Nothing here.") in screen.text
    assert [button.label for button in screen.rows[0]] == [
        "✅ 100000",
        "• ⏸ 0 •",
        "⚠️ 100000",
        "🚫 100000",
    ]
    assert all(display_width(button.label) <= 9 for button in screen.rows[0])
    assert [decode(button.callback) for button in screen.rows[-1]] == [
        Action("add"),
        Action("home"),
    ]
    assert len(screen.rows) == 2


def test_filters_without_counts_show_their_icons() -> None:
    screen = list_text_page(ListPage(filter="a", page=1, pages=1, total=0, items=()))
    assert [button.label for button in screen.rows[0]] == ["• ✅ •", "⏸", "⚠️", "🚫"]


@pytest.mark.parametrize("width", [15, True, "32", 32.0, None, -1])
def test_invalid_row_widths_are_refused(width: Any) -> None:
    with pytest.raises(ValueError, match="width"):
        product_row("Name", Decimal(10), width=width)


def test_a_large_change_is_dropped_before_the_price_overflows() -> None:
    row = product_row("Product", Decimal("999999999999999"), initial=Decimal("1e-20"), width=16)
    assert "<b>1.00E+15 EUR</b>" in row
    assert "▲" not in row
    assert display_width(plain(row)) <= 16


@settings(max_examples=200, deadline=None)
@given(
    st.text(max_size=10000),
    st.sampled_from([None, "0", "19.99", "999999999999999"]),
    st.integers(0, 6),
    st.text(max_size=200),
    st.sampled_from(["en", "it"]),
)
def test_hostile_names_prices_and_notices_stay_safe(
    name: str, current: str | None, count: int, notice: str, locale: str
) -> None:
    set_locale(locale)
    items = tuple(view(n, name, current=current) for n in range(1, count + 1))
    page = ListPage(filter="a", page=1, pages=1, total=count, items=items, counts=(count, 0, 0, 0))
    base = list_text_page(page)
    screen = _with_notice(base, escape(notice))
    assert visible_length(screen.text) <= TELEGRAM_MESSAGE_LIMIT
    assert _is_valid_telegram_markup(screen.text)
    lines = base.text.splitlines()
    rows = [n for n, line in enumerate(lines) if re.match(r"^<b>\d\.</b> ", line)]
    assert len(rows) == count
    for n, index in enumerate(rows, 1):
        assert lines[index].startswith(f"<b>{n}.</b> ")
        assert display_width(plain(lines[index])) <= 34
        assert display_width(plain(lines[index + 1])) <= 34
        assert decode(screen.rows[0][n - 1].callback) == Action("list.open", ("a", 1, n))
    for keyboard_row in screen.rows:
        if len(keyboard_row) == 4:
            assert sum(display_width(button.label) for button in keyboard_row) <= 34
        for button in keyboard_row:
            if button.callback is not None:
                assert len(button.callback.encode("utf-8")) <= 64
                assert decode(button.callback) is not None


@settings(max_examples=200, deadline=None)
@given(
    st.text(max_size=300),
    st.sampled_from([None, Decimal(0), Decimal("1.23"), Decimal("999999999999999")]),
    st.sampled_from([None, Decimal("1e-20"), Decimal("1.23")]),
    st.integers(16, 60),
)
def test_row_budgets_and_default_compatibility(
    name: str, current: Decimal | None, initial: Decimal | None, width: int
) -> None:
    assert display_width(plain(product_row(name, current, initial=initial, width=width))) <= width
    assert product_row(name, current, initial=initial) == _baseline_product_row(
        name, current, initial=initial
    )


def _baseline_product_row(
    name: str,
    current: Decimal | None,
    *,
    initial: Decimal | None = None,
    currency: str = "EUR",
    mark: str = "",
    html: bool = True,
) -> str:
    """Put price/change first; allocate only the remaining display cells to the name.

    Buttons use the same layout as literal text. IDs belong in callbacks and
    list pages. Compact scientific numbers keep extreme amounts/changes
    readable without truncating a monetary value.
    """
    current, initial = amount(current), amount(initial)
    loc = current_locale()
    try:
        price = money(current, currency, locale=loc) if current is not None else _("N/A")
    except ValueError:
        currency = ""
        price = f"{current:.2f}" if current is not None else _("N/A")
    try:
        delta = change(initial, current, locale=loc) if initial and current is not None else ""
    except ValueError:
        delta = _compact_change(initial, current) if initial and current is not None else ""
    delta = delta.replace("▼ ", "▼").replace("▲ ", "▲")
    status = f" {sanitize_label(mark)}" if mark else ""
    extra = f" {delta}" if delta else ""
    if display_width(price + extra) > ROW_WIDTH - 8:
        if current is not None:
            price = f"{current:.2E} {currency}".rstrip()
        if delta and initial and current is not None:
            delta = _compact_change(initial, current)
            extra = f" {delta}"
    status = truncate_to_width(status, max(0, ROW_WIDTH - display_width(price + extra) - 5))
    prefix = price + extra + status
    budget = max(0, ROW_WIDTH - display_width(prefix) - 1)
    shown = truncate_to_width(sanitize_label(name) or _("Unknown"), budget)
    if html:
        prefix = f"<b>{escape(price)}</b>{escape(extra + status)}"
        shown = escape(shown)
    return f"{prefix} {shown}".rstrip()
