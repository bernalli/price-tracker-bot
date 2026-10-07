"""Verifies price_tracker.bot.ui.escape.escape_html."""

from __future__ import annotations

import unicodedata

import pytest
from hypothesis import given, settings

from price_tracker.bot.ui.escape import escape_html
from price_tracker.core.textlimits import _VALID_ENTITY_RE
from tests.ui.conftest import hostile_text


def test_escapes_html_special_characters() -> None:
    assert escape_html('<b>&"x"</b>') == "&lt;b&gt;&amp;&quot;x&quot;&lt;/b&gt;"


def test_flattens_newlines_and_tabs() -> None:
    assert escape_html("a\nb\r\nc\td") == "a b  c d"


def test_flattens_line_and_paragraph_separators() -> None:
    assert escape_html("a\u2028b") == "a b"
    assert escape_html("a\u2029b") == "a b"


def test_leaves_cf_characters_intact() -> None:
    assert escape_html("a\u200db") == "a\u200db"  # ZWJ
    assert escape_html("a\u2066b\u2069c") == "a\u2066b\u2069c"  # FSI/PDI


def test_rejects_non_str() -> None:
    with pytest.raises(TypeError, match=r"."):
        escape_html(42)  # type: ignore[arg-type]


@settings(max_examples=200, deadline=None)
@given(text=hostile_text)
def test_output_never_has_raw_markup_characters_outside_an_entity(text: str) -> None:
    escaped = escape_html(text)
    stripped = _VALID_ENTITY_RE.sub("", escaped)
    assert "<" not in stripped, (text, escaped)
    assert ">" not in stripped, (text, escaped)
    assert '"' not in stripped, (text, escaped)
    assert "&" not in stripped, (text, escaped)


@settings(max_examples=200, deadline=None)
@given(text=hostile_text)
def test_output_never_contains_a_control_character(text: str) -> None:
    escaped = escape_html(text)
    assert all(unicodedata.category(char) != "Cc" for char in escaped), (text, escaped)
