"""Per-code-point display width, cluster-safe truncation and label sanitizing.

``display_width`` targets how Telegram clients actually render emoji and
symbols, not ``unicodedata.east_asian_width`` alone: several single-width
symbols (⏸ ⚠ ⏱ ⚙ 🗑 and the block ranges below) render as two-cell glyphs on
mobile clients even though Unicode's own East Asian Width property calls them
``N`` (Neutral). The rule always overestimates relative to how a client packs
combining sequences, flags and skin-tone modifiers — never underestimates
against a real terminal-width oracle on any *assigned* code point — which is
the safe direction for a budget that must never overflow a button.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator

ZERO_WIDTH_CATEGORIES: Final = frozenset({"Mn", "Me", "Cf"})
WIDE_BLOCKS: Final = (
    (0x2300, 0x23FF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0x4DC0, 0x4DFF),
    (0x1D300, 0x1D356),
    (0x1D360, 0x1D376),  # East Asian Width N in Unicode 15.0, W from 16.0
    (0x1F000, 0x1FAFF),
)
ELLIPSIS: Final = "…"

_VARIATION_SELECTOR_16: Final = "\ufe0f"
_FLATTEN_CATEGORIES: Final = frozenset({"Cc", "Zl", "Zp"})


def _in_wide_block(code_point: int) -> bool:
    return any(low <= code_point <= high for low, high in WIDE_BLOCKS)


def _char_width(text: str, index: int) -> int:
    """The width of the code point at ``index``, per the closed rule list."""
    char = text[index]
    if unicodedata.category(char) in ZERO_WIDTH_CATEGORIES:
        return 0
    if index + 1 < len(text) and text[index + 1] == _VARIATION_SELECTOR_16:
        return 2
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    if _in_wide_block(ord(char)):
        return 2
    return 1


def display_width(text: str) -> int:
    """The visible width Telegram clients render ``text`` at, in cells."""
    return sum(_char_width(text, index) for index in range(len(text)))


def _iter_clusters(text: str) -> Iterator[tuple[int, int]]:
    """Yield ``(start, stop)`` ranges: a base code point plus every following
    combining mark, format character or enclosing mark (``Mn``/``Me``/``Cf``,
    which includes VS16 and ZWJ)."""
    length = len(text)
    index = 0
    while index < length:
        start = index
        index += 1
        while index < length and unicodedata.category(text[index]) in ZERO_WIDTH_CATEGORIES:
            index += 1
        yield start, index


def truncate_to_width(text: str, budget: int) -> str:
    """Truncate to ``budget`` display cells, never splitting base from mark."""
    if budget < 0:
        raise ValueError(f"budget must not be negative, got {budget!r}")
    if display_width(text) <= budget:
        return text
    if budget == 0:
        return ""
    if budget == 1:
        return ELLIPSIS
    limit = budget - 1
    consumed_width = 0
    end = 0
    for start, stop in _iter_clusters(text):
        cluster_width = sum(_char_width(text, index) for index in range(start, stop))
        if consumed_width + cluster_width > limit:
            break
        consumed_width += cluster_width
        end = stop
    return f"{text[:end]}{ELLIPSIS}"


def sanitize_label(text: str) -> str:
    """Strip ``Cf`` (bidi isolates, ZWJ, …), flatten ``Cc``/``Zl``/``Zp`` to a space."""
    kept: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category == "Cf":
            continue
        kept.append(" " if category in _FLATTEN_CATEGORIES else char)
    return "".join(kept).strip()
