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
        assert len(re.findall(r"<a\\s", chunk)) == chunk.count("</a>")


def test_split_message_degrades_an_oversized_single_line() -> None:
    chunks = split_message(f"<b>{'x' * 6000}</b>")

    assert len(chunks) == 1
    assert visible_length(chunks[0]) <= SAFE_LIMIT
    assert "<" not in chunks[0]


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
