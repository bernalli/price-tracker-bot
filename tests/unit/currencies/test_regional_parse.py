"""``parse_regional_price``: detection, cross-check (I13) and the grammar, composed.

The expectation disambiguates a shared symbol; it never fills in a currency
the text never named (C4.4) — that filling arrives with storefronts in a
later change.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from babel.numbers import format_currency, get_currency_precision
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.currencies import RegionalMiss, expected_currencies, parse_regional_price
from price_tracker.core.money import Money

# (text, url, declared, result)
CASES: list[tuple[str, str, frozenset[str] | None, Money | RegionalMiss]] = [
    ("1 234,56 €", "https://shop.example.fr/p", None, Money(Decimal("1234.56"), "EUR")),
    ("1.234,56 €", "https://shop.example.de/p", None, Money(Decimal("1234.56"), "EUR")),
    ("1'234.56", "https://shop.example.ch/p", None, Money(Decimal("1234.56"), None)),
    ("CHF 1'234.56", "https://shop.example.ch/p", None, Money(Decimal("1234.56"), "CHF")),
    ("£1,234.56", "https://shop.example.uk/p", None, Money(Decimal("1234.56"), "GBP")),
    (
        "₹1,23,456.50",
        "https://shop.example.in/p",
        None,
        Money(Decimal("123456.50"), "INR"),
    ),
    ("¥1,234", "https://shop.example.jp/p", None, Money(Decimal("1234"), "JPY")),
    ("¥1,234", "https://shop.example.com/p", None, RegionalMiss("unreadable")),
    ("12,345원", "https://shop.example.kr/p", None, Money(Decimal("12345"), "KRW")),
    ("BHD 1.234", "https://shop.example.bh/p", None, Money(Decimal("1.234"), "BHD")),
    (
        "R$ 1.234,56",
        "https://shop.example.com.br/p",
        None,
        Money(Decimal("1234.56"), "BRL"),
    ),
    ("1 234,56 zł", "https://shop.example.pl/p", None, Money(Decimal("1234.56"), "PLN")),
    ("1.234,56 kr", "https://shop.example.no/p", None, Money(Decimal("1234.56"), "NOK")),
    ("1.234,56 kr", "https://shop.example.se/p", None, Money(Decimal("1234.56"), "SEK")),
    ("1.234,56 kr", "https://shop.example.com/p", None, Money(Decimal("1234.56"), None)),
    ("$12.345", "https://shop.example.com/p", None, RegionalMiss("unreadable")),
    ("$12.345", "https://shop.example.us/p", None, Money(Decimal("12345"), "USD")),
    ("€ 10", "https://shop.example.ch/p", None, RegionalMiss("currency_unexpected")),
    ("TOP 10 deals", "https://shop.example.com/p", None, RegionalMiss("unreadable")),
    ("TOP 10", "https://shop.example.com/p", None, Money(Decimal("10"), "TOP")),
    ("TOP 10", "https://shop.example.de/p", None, RegionalMiss("currency_unexpected")),
    ("$10 USD", "https://shop.example.us/p", None, RegionalMiss("unreadable")),
    ("Rp 10.000", "https://shop.example.id/p", None, RegionalMiss("unreadable")),
    ("$10", "https://shop.example.de/p", None, Money(Decimal("10"), None)),
]


def test_table() -> None:
    for text, url, declared, expected in CASES:
        got = parse_regional_price(text, url=url, declared=declared)
        assert got == expected, f"{text!r} on {url!r}: got {got!r}, want {expected!r}"


# digits/grouping arguments, exercised on top of the regional table above.
FULLWIDTH_DIGITS_CASE = ("￥１，２３４", "fullwidth")
ARAB_DIGITS_CASE = ("١٢٣٤٫٥٦", "arab")


def test_fullwidth_digits_without_a_symbol_is_unreadable() -> None:
    # No currency token in the text at all: a single separator with three digits either
    # side is ambiguous on its own (measured ``None`` on the grammar), and the
    # expectation disambiguates a symbol, it never invents one (C4.4).
    got = parse_regional_price("１，２３４", url="https://shop.example.jp/p", digits="fullwidth")
    assert got == RegionalMiss("unreadable")


def test_fullwidth_digits() -> None:
    text, digits = FULLWIDTH_DIGITS_CASE
    got = parse_regional_price(text, url="https://shop.example.jp/p", digits=digits)
    assert got == Money(Decimal("1234"), "JPY")


def test_arab_digits() -> None:
    text, digits = ARAB_DIGITS_CASE
    got = parse_regional_price(text, url="https://shop.example.eg/p", digits=digits)
    assert got == Money(Decimal("1234.56"), None)


def test_not_well_formed_digits_and_grouping_raise() -> None:
    with pytest.raises(ValueError, match="digit script"):
        parse_regional_price("10", url="https://shop.example.de/p", digits="roman")
    with pytest.raises(ValueError, match="grouping"):
        parse_regional_price("10", url="https://shop.example.de/p", grouping="chinese")


_LOCALE_CURRENCY: dict[str, str] = {
    "de_DE": "EUR",
    "fr_FR": "EUR",
    "it_IT": "EUR",
    "es_ES": "EUR",
    "nl_NL": "EUR",
    "en_GB": "GBP",
    "en_IN": "INR",
    "ja_JP": "JPY",
    "pt_BR": "BRL",
    "pl_PL": "PLN",
    "nb_NO": "NOK",
    "sv_SE": "SEK",
    "de_CH": "CHF",
    "en_US": "USD",
    "es_MX": "MXN",
    "ko_KR": "KRW",
    "uk_UA": "UAH",
    "zh_Hans_CN": "CNY",
    "en_AU": "AUD",
}


@st.composite
def _locale_currency_amount(draw: st.DrawFn) -> tuple[str, str, Decimal]:
    locale_id, currency = draw(st.sampled_from(sorted(_LOCALE_CURRENCY.items())))
    precision = get_currency_precision(currency)
    amount = draw(
        st.decimals(
            min_value=Decimal(1),
            max_value=Decimal(999_999),
            places=precision,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    return locale_id, currency, amount


@given(data=_locale_currency_amount())
@settings(max_examples=300, deadline=None)
def test_roundtrip_through_babel_formatting(data: tuple[str, str, Decimal]) -> None:
    locale_id, currency, amount = data
    text = format_currency(amount, currency, locale=locale_id)
    result = parse_regional_price(
        text, url="https://shop.example.com/p", declared=frozenset({currency})
    )
    assert result == Money(amount, currency), (locale_id, currency, amount, text, result)


@given(
    text=st.one_of(st.text(max_size=80), st.none(), st.binary(max_size=20), st.integers()),
    url=st.one_of(st.text(max_size=80), st.none(), st.integers()),
)
@settings(max_examples=300, deadline=None)
def test_totality(text: object, url: object) -> None:
    result = parse_regional_price(text, url=url)
    assert isinstance(result, Money | RegionalMiss)
    if isinstance(result, Money) and result.currency is not None:
        expected = expected_currencies(url)
        if expected:
            assert result.currency in expected
