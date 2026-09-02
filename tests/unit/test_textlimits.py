"""Property tests for Telegram-safe text segmentation."""

from __future__ import annotations

import re

import pytest

from price_tracker.core.textlimits import (
    SAFE_LIMIT,
    paginate,
    split_message,
    truncate_visible,
    visible_length,
)


def test_visible_length_strips_tags_and_unescapes_entities() -> None:
    assert visible_length("• <b>A &amp; B</b> — <code>x &lt; y</code>") == 15


@pytest.mark.parametrize(
    ("text", "budget", "expected"), [("short", 10, "short"), ("abcdef", 4, "abc…")]
)
def test_truncate_visible_respects_budget(text: str, budget: int, expected: str) -> None:
    assert truncate_visible(text, budget) == expected


def test_split_message_preserves_balanced_rows_under_safe_limit() -> None:
    lines = [f"<b>{index:03d}-{'x' * 196}</b>" for index in range(300)]
    chunks = split_message("\n".join(lines))

    assert chunks
    assert all(visible_length(chunk) <= SAFE_LIMIT for chunk in chunks)
    assert "\n".join(chunks) == "\n".join(lines)
    for chunk in chunks:
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<code>") == chunk.count("</code>")
        assert len(re.findall(r"<a\s", chunk)) == chunk.count("</a>")


def test_split_message_degrades_an_oversized_single_line() -> None:
    chunks = split_message(f"<b>{'x' * 6000}</b>")

    assert len(chunks) == 1
    assert visible_length(chunks[0]) <= SAFE_LIMIT
    assert "<" not in chunks[0]
    assert chunks[0].startswith("x" * (SAFE_LIMIT - 1))
    assert chunks[0].endswith("…")


def test_textlimit_boundary_guards() -> None:
    assert truncate_visible("abc", 0) == ""
    assert truncate_visible("abc", 1) == "…"
    with pytest.raises(ValueError, match="negative"):
        truncate_visible("abc", -1)
    for limit in (0, -1):
        with pytest.raises(ValueError, match="positive"):
            split_message("x", limit=limit)
    for limit in (0, 1, 2):
        with pytest.raises(ValueError, match="leave room"):
            paginate("h", [(1, "b")], "f", limit=limit)


def test_split_message_exercises_every_supported_test_tag() -> None:
    text = '<b>bold</b>\n<code>code</code>\n<a href="https://example.com">link</a>'
    chunks = split_message(text, limit=10)

    assert "\n".join(chunks) == text
    for chunk in chunks:
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<code>") == chunk.count("</code>")
        assert len(re.findall(r"<a\s", chunk)) == chunk.count("</a>")


def test_split_message_empty_short_blank_and_entities() -> None:
    assert split_message("") == []
    assert split_message("<b>short</b>") == ["<b>short</b>"]
    assert split_message("one\n\ntwo") == ["one\n\ntwo"]
    assert split_message("&amp;" * 3000, limit=3000) == ["&amp;" * 3000]


def test_paginate_preserves_keys_order_and_page_envelopes() -> None:
    header = "H" * 200
    footer = "F" * 100
    blocks = [(index, "B" * 300) for index in range(50)]
    pages = paginate(header, blocks, footer)

    assert pages
    assert all(visible_length(text) <= SAFE_LIMIT for text, _ in pages)
    assert all(text.startswith(header) and text.endswith(footer) for text, _ in pages)
    assert [key for _, keys in pages for key in keys] == list(range(50))


def test_paginate_degrades_large_block_and_returns_no_page_without_blocks() -> None:
    pages = paginate("header", [(1, "<b>" + "x" * 5000 + "</b>")], "footer")

    assert len(pages) == 1
    assert pages[0][1] == [1]
    assert visible_length(pages[0][0]) <= SAFE_LIMIT
    assert "<b>" not in pages[0][0]
    assert paginate("header", [], "footer") == []


def test_paginate_degrades_an_overlong_envelope_and_reserves_block_room() -> None:
    pages = paginate("H" * 100, [(1, "BLOCK")], "F" * 100, limit=20)

    assert len(pages) == 1
    assert pages[0][1] == [1]
    assert visible_length(pages[0][0]) <= 20
    assert "\n…\n" in pages[0][0]


def test_split_message_degrades_a_tag_that_is_never_closed() -> None:
    """A row that opens a tag and never closes it must not reach Telegram raw."""
    chunks = split_message("<b>opened forever")

    assert chunks == ["opened forever"]


def test_split_message_degrades_a_lone_closing_tag() -> None:
    """A row with only a closing tag, no matching open, must be degraded."""
    chunks = split_message("closed early</b>")

    assert chunks == ["closed early"]


def test_split_message_degrades_cross_nested_tags() -> None:
    """``<b><i>x</b></i>`` closes out of order and must be degraded, not passed through."""
    chunks = split_message("<b><i>mixed</b></i>")

    assert chunks == ["mixed"]


def test_split_message_degrades_a_bare_less_than_sign() -> None:
    """A ``<`` that does not open a recognizable tag must still be degraded."""
    chunks = split_message("5 < 10 apples")

    assert chunks == ["5 &lt; 10 apples"]


def test_split_message_degrades_an_unknown_tag() -> None:
    """A tag outside Telegram's supported subset must be degraded."""
    chunks = split_message("<foo>bar</foo>")

    assert chunks == ["bar"]


def test_split_message_degrades_an_incomplete_entity() -> None:
    """An entity-like sequence missing its trailing ``;`` must be degraded.

    This is the review's exact reproduction: before the grammar guard,
    ``split_message("x &amp")`` returned the row unchanged because its
    unescaped visible length stayed under the limit.
    """
    chunks = split_message("x &amp")

    assert chunks == ["x &amp;"]


def test_split_message_degrades_ten_thousand_fake_tags_with_zero_visible_length() -> None:
    """A row of unsupported tags with visible_length == 0 must still be degraded.

    Before the grammar guard, ``visible_length("<foo>" * 10000) == 0`` meant
    the oversize check never fired and the raw markup was returned as-is.
    """
    text = "<foo>" * 10000

    assert visible_length(text) == 0
    chunks = split_message(text)

    assert "<foo>" not in "".join(chunks)
    assert "<" not in "".join(chunks)


def test_paginate_degrades_a_block_with_malformed_markup() -> None:
    """A block that fits the room budget but fails the grammar is still degraded."""
    pages = paginate("header", [(1, "<b>unterminated")], "footer")

    assert len(pages) == 1
    assert pages[0][1] == [1]
    assert "<b>" not in pages[0][0]
    assert "unterminated" in pages[0][0]


def test_split_message_degrades_a_bare_greater_than_sign() -> None:
    """Telegram wants ">" escaped as "&gt;": a bare one must not reach the wire."""
    out = "".join(split_message("<b>maths</b> 5 > 3"))

    assert "5 &gt; 3" in out
    assert "5 > 3" not in out
    assert "<b>" not in out


def test_split_message_degrades_nested_blockquotes() -> None:
    """Telegram rejects a blockquote nested inside another blockquote."""
    chunks = split_message("<blockquote><blockquote>quoted</blockquote></blockquote>")

    assert "<blockquote>" not in "".join(chunks)
    assert "quoted" in "".join(chunks)


def test_paginate_degrades_a_header_or_footer_with_malformed_markup() -> None:
    """A broken envelope would break every page, so it is degraded too."""
    pages = paginate("<b>broken header", [(1, "block")], "footer </i>")

    assert len(pages) == 1
    assert "<b>" not in pages[0][0]
    assert "</i>" not in pages[0][0]
    assert "broken header" in pages[0][0]
    assert "footer" in pages[0][0]
