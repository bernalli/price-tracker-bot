"""Property tests for the wired ``parse_price``/``detect_currency`` wrapper (D10, §4.1.7).

Out of the harvest plugin's default perimeter (``--tests tests/unit``): these generate
inputs with ``hypothesis`` and are not meant to grow the parity corpus.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.currency_symbols import SYMBOLS
from price_tracker.core.money import ACCEPTED_CURRENCIES, significant_fraction_digits
from price_tracker.core.scraper_base import detect_currency, parse_price


@settings(max_examples=300, deadline=None)
@given(st.text())
def test_parse_price_is_total_and_bounded(text: str) -> None:
    result = parse_price(text)
    if result is None:
        return
    assert result.is_finite()
    assert Decimal(0) < result <= Decimal(10) ** 9
    assert significant_fraction_digits(result) <= 3


@settings(max_examples=300, deadline=None)
@given(
    st.decimals(
        min_value=Decimal("0.01"),
        max_value=Decimal("999999999"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_parse_price_reads_back_str_of_decimal(amount: Decimal) -> None:
    assert parse_price(str(amount)) == amount


@settings(max_examples=300, deadline=None)
@given(st.text())
def test_detect_currency_is_total(text: str) -> None:
    result = detect_currency(text)
    assert result is None or result in ACCEPTED_CURRENCIES


@settings(max_examples=300, deadline=None)
@given(
    st.one_of(
        st.from_regex(r"\A[0-9.,' ]{1,30}\Z"),
        st.decimals(
            min_value=Decimal("0.01"),
            max_value=Decimal("999999999"),
            places=2,
            allow_nan=False,
            allow_infinity=False,
        ).map(str),
    )
)
def test_detect_currency_never_defaults_on_digits(text: str) -> None:
    assert detect_currency(text) is None


def test_detect_currency_shared_symbol_is_none_for_every_key() -> None:
    """A symbol shared by several currencies never defaults; one accepted key resolves."""
    for key, codes in SYMBOLS.items():
        accepted = codes & ACCEPTED_CURRENCIES
        if len(accepted) > 1:
            assert detect_currency(f"{key}10") is None, key
            assert detect_currency(f"10 {key}") is None, key
        elif len(accepted) == 1:
            (expected,) = accepted
            assert detect_currency(f"10 {key}") == expected, key
