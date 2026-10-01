"""`_parse_id` accepts only a positive id written in ASCII digits.

Every command argument and callback payload that names a product or a user goes
through `_parse_id`. It used to strip whitespace, drop every `#` and hand the
rest to `int()`, which also accepts signs, underscores, non-ASCII digits and
numbers too large for an SQLite INTEGER.
"""

from __future__ import annotations

import pytest

from price_tracker.bot.handlers._helpers import ID_MAX, _parse_id

WELL_FORMED: list[tuple[str, int]] = [
    ("1", 1),
    ("42", 42),
    ("#42", 42),
    ("007", 7),
    (str(ID_MAX), ID_MAX),
]

NOT_WELL_FORMED: list[str] = [
    "",
    "#",
    "0",
    "#0",
    "-1",
    "+1",
    "1e3",
    "0x10",
    "1_000",
    "1.0",
    " 12",
    "12 ",
    "1 2",
    "12\n",
    "##12",
    "1#2",
    "12#",
    "١٢",  # ARABIC-INDIC DIGIT ONE, TWO
    "１２",  # FULLWIDTH DIGIT ONE, TWO
    "²",  # SUPERSCRIPT TWO
    str(ID_MAX + 1),
    "9" * 40,
    "abc",
]


@pytest.mark.parametrize(("text", "expected"), WELL_FORMED)
def test_parse_id_accepts_a_positive_ascii_id(text: str, expected: int) -> None:
    assert _parse_id(text) == expected


@pytest.mark.parametrize("text", NOT_WELL_FORMED, ids=ascii)
def test_parse_id_rejects_not_well_formed_input(text: str) -> None:
    assert _parse_id(text) is None
