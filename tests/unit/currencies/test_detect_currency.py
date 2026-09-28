"""``detect_currency`` (D5): same tokenizer as the grammar, at most two tokens.

The function never invents a currency: a unique signal wins regardless of the
caller's expectation, two signals that disagree yield ``None``, and only a
genuinely ambiguous shared symbol is disambiguated by the expectation.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core import pricegrammar
from price_tracker.core.currencies import ISO_CURRENCIES, detect_currency
from price_tracker.core.currency_symbols import SYMBOLS

# (text, expected, result)
CASES: list[tuple[str, frozenset[str], str | None]] = [
    ("", frozenset(), None),
    ("12345", frozenset(), None),
    ("ABC 10", frozenset(), None),
    ("XTS 10", frozenset(), None),
    ("XXX 10", frozenset(), None),
    ("$10", frozenset(), None),
    ("10 kr", frozenset({"NOK"}), "NOK"),
    ("10 kr", frozenset({"NOK", "SEK"}), None),
    ("10 kr", frozenset({"EUR"}), None),
    ("10 kr.", frozenset({"DKK"}), "DKK"),
    ("10 kr.", frozenset({"SEK"}), None),
    ("€ 10 USD", frozenset(), None),
    ("$10 USD", frozenset(), "USD"),
    ("USD 10 €", frozenset(), None),
    ("R$ 10", frozenset(), "BRL"),
    ("R$ 10", frozenset({"USD"}), "BRL"),  # the long symbol wins, P16
    ("CA$ 10", frozenset(), "CAD"),
    ("MOP$ 10", frozenset(), None),  # section 4(d)
    ("10 MOP$", frozenset(), "MOP"),  # section 4(d)
    ("$USD 10", frozenset(), "USD"),
    ("€10 ₽", frozenset(), None),
    ("TOP 10", frozenset(), "TOP"),  # section 4(e), pinned as a known limit
    ("TOP 10", frozenset({"EUR"}), "TOP"),
    ("EUR10", frozenset(), "EUR"),
    ("x" * 64 + "€", frozenset(), None),  # 65 characters
    ("   ", frozenset(), None),
    (" ", frozenset(), None),
    ("EURO 10", frozenset(), None),  # ISO token glued to a letter is rejected
]


def test_kr_without_expectation_is_none() -> None:
    assert detect_currency("10 kr") is None


@pytest.mark.parametrize(("text", "expected", "result"), CASES, ids=[repr(c[0]) for c in CASES])
def test_table(text: str, expected: frozenset[str], result: str | None) -> None:
    assert detect_currency(text, expected=expected) == result


def test_bad_expected_raises() -> None:
    with pytest.raises(ValueError, match="bad_expected"):
        detect_currency("10", expected={"EUR"})  # type: ignore[arg-type]  # a set, not a frozenset
    with pytest.raises(ValueError, match="bad_expected"):
        detect_currency("10", expected=frozenset({"eur"}))
    with pytest.raises(ValueError, match="bad_expected"):
        detect_currency("10", expected=frozenset({"XTS"}))


def _rule_outcome(candidates: frozenset[str], expected: frozenset[str]) -> str | None:
    """Independent re-derivation of the section 3.4 step-3 rule."""
    if len(candidates) == 1:
        return next(iter(candidates))
    intersection = candidates & expected
    return next(iter(intersection)) if len(intersection) == 1 else None


SHARED_SYMBOL_KEYS = tuple(sorted(key for key, value in SYMBOLS.items() if len(value) > 1))


@pytest.mark.parametrize("key", SHARED_SYMBOL_KEYS)
def test_shared_symbol_combinatorics(key: str) -> None:
    """Every shared symbol, crossed with every class of expectation size."""
    candidates = SYMBOLS[key]
    text = f"10{key}"
    members = sorted(candidates)
    member = members[0]
    non_member = next(code for code in sorted(ISO_CURRENCIES) if code not in candidates)
    two_members = frozenset(members[:2])
    expectations = [
        frozenset(),
        frozenset({member}),
        frozenset({non_member}),
        two_members,
    ]
    for expected in expectations:
        want = _rule_outcome(candidates, expected)
        got = detect_currency(text, expected=expected)
        assert got == want, (key, text, sorted(expected), got, want)


@given(
    text=st.one_of(
        st.text(max_size=80),
        st.none(),
        st.binary(max_size=20),
        st.integers(),
        st.floats(allow_nan=True),
    )
)
@settings(max_examples=300, deadline=None)
def test_never_raises_and_result_is_a_known_currency_or_none(text: object) -> None:
    result = detect_currency(text)
    assert result is None or result in ISO_CURRENCIES


@st.composite
def _currency_bearing_text(draw: st.DrawFn) -> str:
    amount = draw(st.integers(min_value=1, max_value=999_999))
    token = draw(st.sampled_from(sorted(SYMBOLS) + sorted(ISO_CURRENCIES)))
    at_start = draw(st.booleans())
    return f"{token}{amount}" if at_start else f"{amount}{token}"


@given(text=_currency_bearing_text(), extra=st.frozensets(st.sampled_from(sorted(ISO_CURRENCIES))))
@settings(max_examples=300, deadline=None)
def test_a_unique_signal_does_not_depend_on_the_expectation(
    text: str, extra: frozenset[str]
) -> None:
    baseline = detect_currency(text)
    if baseline is None:
        return
    assert detect_currency(text, expected=extra) == baseline


@given(
    text=_currency_bearing_text(), expected=st.frozensets(st.sampled_from(sorted(ISO_CURRENCIES)))
)
@settings(max_examples=300, deadline=None)
def test_result_is_admitted_by_the_first_token(text: str, expected: frozenset[str]) -> None:
    result = detect_currency(text, expected=expected)
    if result is None:
        return
    _residue, iso, candidates = pricegrammar._strip_currency_token(text.strip())
    if iso is not None:
        assert result == iso
    else:
        assert candidates is not None
        assert result in candidates
