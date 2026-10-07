"""Verifies price_tracker.bot.ui.width: display width, truncation, sanitizing."""

from __future__ import annotations

import unicodedata

import pytest
import wcwidth
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.bot.ui.width import display_width, sanitize_label, truncate_to_width
from tests.ui.conftest import hostile_text

# Known divergences between display_width (price_tracker.bot.ui.width) and
# the wcwidth 0.9.1 oracle. display_width never *under*estimates on any
# single assigned, non-Cf code point
# (test_display_width_never_below_wcwidth_on_every_assigned_code_point proves
# that exhaustively); these are the classes where it *over*estimates, or
# where the two tables disagree only on a Cf code point that display_width
# counts as zero-width and wcwidth counts as one cell.
KNOWN_DISAGREEMENTS: dict[str, tuple[int, int]] = {
    # The three East-Asian-Width-Neutral wide blocks: display_width counts every code
    # point in range 2; wcwidth's 18.0.0 tables give the unassigned/So ones 1.
    "U+2300 (misc technical block)": (display_width("⌀"), wcwidth.wcswidth("⌀")),
    "U+2600 (misc symbols block)": (display_width("☀"), wcwidth.wcswidth("☀")),
    "U+2B00 (misc symbols/arrows block)": (display_width("⬀"), wcwidth.wcswidth("⬀")),
    # A flag (two regional indicators): display_width sums each indicator at 2 (both are
    # inside the wide emoji block); wcwidth treats the pair as one 2-cell
    # glyph.
    "regional indicator pair (flag)": (
        display_width("\U0001f1ee\U0001f1f9"),
        wcwidth.wcswidth("\U0001f1ee\U0001f1f9"),
    ),
    # An emoji plus a skin-tone modifier: display_width sums both at 2 each; wcwidth
    # renders the modifier as part of one 2-cell glyph.
    "emoji + skin-tone modifier": (
        display_width("\U0001f44b\U0001f3fd"),
        wcwidth.wcswidth("\U0001f44b\U0001f3fd"),
    ),
}

# Cf code points where raw wcwidth.wcwidth() counts 1 cell and display_width
# counts 0. Known limit: the "never underestimate" guarantee is scoped to
# wcwidth_oracle(), which strips every Cf before comparing (a Cf is never
# rendered as a visible cell in the first place), so these do not violate
# it — they are exactly why Cf is stripped.
KNOWN_CF_UNDERESTIMATES: dict[str, tuple[int, int]] = {
    "U+00AD soft hyphen (Cf)": (display_width("\xad"), wcwidth.wcwidth("\xad")),
    "U+0600 Arabic number sign (Cf)": (display_width("\u0600"), wcwidth.wcwidth("\u0600")),
    "U+070F Syriac abbreviation mark (Cf)": (
        display_width("\u070f"),
        wcwidth.wcwidth("\u070f"),
    ),
}


def _strip_invisible(text: str) -> str:
    return "".join(
        char for char in text if unicodedata.category(char) not in ("Cf", "Cc", "Zl", "Zp")
    )


def wcwidth_oracle(text: str) -> int:
    return wcwidth.wcswidth(_strip_invisible(text))


# --- display_width: pinned table ---------------------------------------------


@pytest.mark.parametrize(
    ("char", "expected"),
    [
        ("⏸", 2),  # ⏸ PAUSE
        ("⚠", 2),  # ⚠ WARNING SIGN
        ("⏱", 2),  # ⏱ STOPWATCH
        ("⚙", 2),  # ⚙ GEAR
        ("\U0001f5d1", 2),  # 🗑 WASTEBASKET
        ("◀", 1),  # ◀ BLACK LEFT-POINTING TRIANGLE
        ("◀️", 2),  # ◀️ with VS16
        ("▼", 1),  # ▼
        ("·", 1),  # ·
        ("…", 1),  # …
        ("é", 1),  # é (precomposed)
        ("e\u0301", 1),  # e + combining acute
        ("\u200d", 0),  # ZWJ
        ("\u2068", 0),  # FSI
        ("\xa0", 1),  # NBSP
        ("￥", 2),  # ￥ fullwidth yen
        ("¥", 1),  # ¥ yen sign
        ("電", 2),  # 電
    ],
)
def test_display_width_pinned_table(char: str, expected: int) -> None:
    assert display_width(char) == expected


def test_known_disagreements_are_all_overestimates_or_equal() -> None:
    for name, (ours, oracle) in KNOWN_DISAGREEMENTS.items():
        assert ours >= oracle, name


def test_known_cf_underestimates_stay_zero_and_do_not_regress() -> None:
    for name, (ours, oracle) in KNOWN_CF_UNDERESTIMATES.items():
        assert ours == 0, name
        assert oracle == 1, name


# --- display_width: additivity property --------------------------------------


@settings(max_examples=200, deadline=None)
@given(left=hostile_text, right=hostile_text)
def test_display_width_is_additive_unless_the_join_forms_a_cluster(left: str, right: str) -> None:
    if not right:
        return
    first = right[0]
    if first == "\ufe0f" or unicodedata.category(first) in ("Mn", "Me", "Cf"):
        return
    assert display_width(left + right) == display_width(left) + display_width(right)


# --- display_width: never below the wcwidth oracle ---------------------------


@settings(max_examples=200, deadline=None)
@given(text=hostile_text)
def test_display_width_never_below_wcwidth_oracle(text: str) -> None:
    oracle = wcwidth_oracle(text)
    assert oracle >= 0, (text, oracle)
    assert display_width(text) >= oracle, (text, display_width(text), oracle)


_ASCII_CJK_HANGUL = st.text(
    alphabet=st.one_of(
        st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF),
        st.characters(min_codepoint=0xAC00, max_codepoint=0xD7A3),
    ),
    min_size=0,
    max_size=100,
)


@settings(max_examples=200, deadline=None)
@given(text=_ASCII_CJK_HANGUL)
def test_display_width_equals_oracle_on_ascii_cjk_hangul(text: str) -> None:
    assert display_width(text) == wcwidth_oracle(text)


def test_display_width_never_below_wcwidth_on_every_assigned_code_point() -> None:
    for code_point in range(0x110000):
        if 0xD800 <= code_point <= 0xDFFF:
            continue
        char = chr(code_point)
        category = unicodedata.category(char)
        if category in ("Cn", "Cf"):
            continue
        ours = display_width(char)
        oracle = wcwidth.wcwidth(char)
        assert ours >= oracle, (
            f"first failing code point U+{code_point:04X} ({category}): "
            f"display_width={ours} wcwidth={oracle}"
        )


# --- truncate_to_width: budget and cluster properties -------------------------


def test_truncate_fits_unchanged() -> None:
    assert truncate_to_width("hi", 10) == "hi"


def test_truncate_budget_zero() -> None:
    assert truncate_to_width("hello", 0) == ""


def test_truncate_budget_one_uses_ellipsis() -> None:
    assert truncate_to_width("hello", 1) == "…"


def test_truncate_negative_budget_rejected() -> None:
    with pytest.raises(ValueError, match=r"."):
        truncate_to_width("hi", -1)


@pytest.mark.parametrize(
    ("budget", "expected"),
    [(3, "ab…"), (4, "ab…"), (5, "ab⚠️…")],
)
def test_truncate_warning_emoji_boundary(budget: int, expected: str) -> None:
    assert truncate_to_width("ab⚠️cd", budget) == expected


@settings(max_examples=200, deadline=None)
@given(text=hostile_text, budget=st.integers(min_value=0, max_value=40))
def test_truncate_never_exceeds_budget(text: str, budget: int) -> None:
    out = truncate_to_width(text, budget)
    assert display_width(out) <= budget, (text, budget, out)


@settings(max_examples=200, deadline=None)
@given(text=hostile_text, budget=st.integers(min_value=0, max_value=40))
def test_truncate_ellipsis_ends_on_a_cluster_boundary(text: str, budget: int) -> None:
    out = truncate_to_width(text, budget)
    if not out.endswith("…") or out == "…":
        return
    prefix = out[:-1]
    assert text.startswith(prefix), (text, budget, out)
    if len(text) > len(prefix):
        next_char = text[len(prefix)]
        assert next_char != "\ufe0f"
        assert unicodedata.category(next_char) not in ("Mn", "Me", "Cf")


@settings(max_examples=200, deadline=None)
@given(
    prefix_length=st.integers(min_value=0, max_value=40),
    budget=st.integers(min_value=0, max_value=40),
)
def test_truncate_ascii_prefix_width_non_decreasing(prefix_length: int, budget: int) -> None:
    text = "a" * prefix_length
    shorter = truncate_to_width(text, budget)
    longer = truncate_to_width(text + "a", budget)
    assert display_width(longer) >= display_width(shorter) or display_width(longer) == budget


# --- sanitize_label ------------------------------------------------------------


def test_sanitize_strips_bidi_isolates() -> None:
    assert sanitize_label("\u2068hi\u2069") == "hi"


def test_sanitize_strips_lrm() -> None:
    assert sanitize_label("\u200e") == ""


def test_sanitize_strips_zwj() -> None:
    assert sanitize_label("a\u200db") == "ab"


def test_sanitize_flattens_newline_and_tab() -> None:
    assert sanitize_label("a\nb\tc") == "a b c"


def test_sanitize_strips_surrounding_whitespace() -> None:
    assert sanitize_label("  x  ") == "x"


def test_sanitize_does_not_truncate() -> None:
    long_label = "a" * 200
    assert sanitize_label(long_label) == long_label
