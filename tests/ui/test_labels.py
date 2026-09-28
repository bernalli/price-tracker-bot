"""Verifies price_tracker.bot.ui.labels: button() and the budgeted row layout."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.bot.ui.labels import HALF_WIDTH, ROW_WIDTH, button, layout_rows
from price_tracker.bot.ui.width import display_width

if TYPE_CHECKING:
    from price_tracker.bot.ui.screens import Button

# --- button ------------------------------------------------------------------


def test_button_rejects_neither_callback_nor_url() -> None:
    with pytest.raises(ValueError, match=r"."):
        button("x")


def test_button_rejects_both_callback_and_url() -> None:
    with pytest.raises(ValueError, match=r"."):
        button("x", callback="a", url="u")


@pytest.mark.parametrize("callback", ["", "a" * 65, "a b", "é", "a\n"])
def test_button_rejects_not_well_formed_callback(callback: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        button("x", callback=callback)


def test_button_rejects_empty_url() -> None:
    with pytest.raises(ValueError, match=r"."):
        button("x", url="")


def test_button_rejects_text_that_sanitizes_to_empty() -> None:
    with pytest.raises(ValueError, match=r"."):
        button("‎\n", callback="a")


@pytest.mark.parametrize("length", [1, 64])
def test_button_accepts_boundary_callback_lengths(length: int) -> None:
    result = button("x", callback="a" * length)
    assert result.callback == "a" * length


def test_button_accepts_any_non_empty_url() -> None:
    result = button("x", url="anything")
    assert result.url == "anything"


def test_button_label_is_not_truncated() -> None:
    long_text = "x" * 200
    result = button(long_text, callback="a")
    assert result.label == long_text


# --- layout_rows: worked examples --------------------------------------------


def test_layout_rows_two_short_buttons_share_a_row() -> None:
    a, b = button("a", callback="a"), button("b", callback="b")
    rows = layout_rows([a, b])
    assert rows == ((a, b),)


def test_layout_rows_wide_button_first_takes_its_own_row() -> None:
    a, b = button("a", callback="a"), button("b", callback="b")
    wide = button("x" * 20, callback="c")
    rows = layout_rows([wide, a, b])
    assert [tuple(btn.callback for btn in row) for row in rows] == [("c",), ("a", "b")]


def test_layout_rows_wide_button_in_the_middle_splits_the_group() -> None:
    a, b = button("a", callback="a"), button("b", callback="b")
    wide = button("x" * 20, callback="c")
    rows = layout_rows([a, wide, b])
    assert [tuple(btn.callback for btn in row) for row in rows] == [("a",), ("c",), ("b",)]


def test_layout_rows_truncates_a_solo_button_wider_than_row_width() -> None:
    over_budget = button("x" * 60, callback="c")
    rows = layout_rows([over_budget])
    assert len(rows) == 1
    assert len(rows[0]) == 1
    assert display_width(rows[0][0].label) <= ROW_WIDTH


def test_layout_rows_empty_group_produces_no_rows() -> None:
    assert layout_rows([]) == ()


def test_layout_rows_packs_groups_independently_and_in_order() -> None:
    a, b, c = button("a", callback="a"), button("b", callback="b"), button("c", callback="c")
    rows = layout_rows([a], [b, c])
    assert [tuple(btn.callback for btn in row) for row in rows] == [("a",), ("b", "c")]


def test_layout_rows_never_crosses_a_group_boundary() -> None:
    a = button("a", callback="a")
    b, c = button("b", callback="b"), button("c", callback="c")
    rows = layout_rows([a], [b, c])
    row_callbacks = [tuple(btn.callback for btn in row) for row in rows]
    assert ("a", "b") not in row_callbacks


# --- layout_rows: property 5 --------------------------------------------------

_LABEL_TEXT = st.text(
    alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), min_size=1, max_size=60
).map(lambda s: s if s.strip() else "x")


@st.composite
def _button_groups(draw: st.DrawFn) -> list[list[Button]]:
    total = draw(st.integers(min_value=0, max_value=12))
    group_count = draw(st.integers(min_value=1, max_value=3))
    labels = [draw(_LABEL_TEXT) for _ in range(total)]
    buttons = [button(label, callback=f"cb{index}") for index, label in enumerate(labels)]
    groups: list[list[Button]] = [[] for _ in range(group_count)]
    for index, btn in enumerate(buttons):
        groups[index % group_count].append(btn)
    return groups


@settings(max_examples=200, deadline=None)
@given(groups=_button_groups())
def test_layout_rows_property(groups: list[list[Button]]) -> None:
    input_buttons = [btn for group in groups for btn in group]
    rows = layout_rows(*groups)

    output_buttons: list[Button] = []
    for row in rows:
        assert len(row) in (1, 2), row
        if len(row) == 2:
            assert display_width(row[0].label) <= HALF_WIDTH, row
            assert display_width(row[1].label) <= HALF_WIDTH, row
        else:
            assert display_width(row[0].label) <= ROW_WIDTH, row
        output_buttons.extend(row)

    def token(btn: Button) -> str:
        assert btn.callback is not None
        return btn.callback

    input_tokens = sorted(token(btn) for btn in input_buttons)
    output_tokens = sorted(token(btn) for btn in output_buttons)
    assert input_tokens == output_tokens

    # Order is preserved within the flattened row sequence.
    assert [btn.callback for btn in output_buttons] == [btn.callback for btn in input_buttons]
