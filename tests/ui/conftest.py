"""Shared fixtures and generators for the bot.ui / i18n rendering test suite."""

from __future__ import annotations

from typing import Final

from hypothesis import strategies as st

# Fixed code point ranges, stable between Unicode 14 and 15.1 (P15), so a
# hostile string generated on one CI matrix leg means the same thing on
# another: ASCII printable (includes < > & " '), CJK, hiragana/katakana,
# hangul, Arabic, Hebrew, three emoji blocks, skin-tone modifiers, ZWJ, VS16,
# combining marks, bidi controls and isolates, NBSP/NNBSP, \n \r \t, and the
# line/paragraph separators.
_HOSTILE_RANGES: Final = (
    (0x20, 0x7E),
    (0x4E00, 0x9FFF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7A3),
    (0x0621, 0x064A),
    (0x05D0, 0x05EA),
    (0x1F300, 0x1F5FF),
    (0x1F600, 0x1F64F),
    (0x1F900, 0x1F9FF),
    (0x1F3FB, 0x1F3FF),
    (0x200D, 0x200D),
    (0xFE0F, 0xFE0F),
    (0x0300, 0x036F),
    (0x202A, 0x202E),
    (0x2066, 0x2069),
    (0x00A0, 0x00A0),
    (0x202F, 0x202F),
    (0x000A, 0x000A),
    (0x000D, 0x000D),
    (0x0009, 0x0009),
    (0x2028, 0x2029),
)

hostile_text = st.text(
    alphabet=st.one_of(
        *(st.characters(min_codepoint=low, max_codepoint=high) for low, high in _HOSTILE_RANGES)
    ),
    min_size=0,
    max_size=300,
)
