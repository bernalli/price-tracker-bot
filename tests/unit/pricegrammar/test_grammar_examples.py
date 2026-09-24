"""Example rows of the price grammar: the normative acceptance and rejection tables.

Every row is written from the price-core specification, not from the parser's output.
Invisible separators are built with ``chr`` so that the source shows which one is used.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.identity import RequestedIdentity
from price_tracker.core.money import Money
from price_tracker.core.pricegrammar import (
    PriceContext,
    PriceKind,
    PriceText,
    Unreadable,
    decode_price_value,
    parse_price_text,
    parse_price_value,
    select_offer,
)
from price_tracker.core.structured_data import anchor_structured_page

NBSP = chr(0x00A0)
NNBSP = chr(0x202F)
THIN = chr(0x2009)
RSQUO = chr(0x2019)
EN_DASH = chr(0x2013)
MINUS = chr(0x2212)
ARABIC_DECIMAL = chr(0x066B)
ARABIC_GROUPING = chr(0x066C)
FW_COMMA = chr(0xFF0C)


def _arab(text: str) -> str:
    return "".join(chr(0x0660 + int(c)) if c.isdigit() else c for c in text)


def _fullwidth(text: str) -> str:
    return "".join(chr(0xFF10 + int(c)) if c.isdigit() else c for c in text)


def _deva(text: str) -> str:
    return "".join(chr(0x0966 + int(c)) if c.isdigit() else c for c in text)


ACCEPT: list[tuple[str, PriceContext, str]] = [
    ("1.234,56", PriceContext(), "1234.56"),
    ("1,234.56", PriceContext(), "1234.56"),
    ("1.234.567,89", PriceContext(), "1234567.89"),
    ("1 234,56", PriceContext(), "1234.56"),
    (f"1{NBSP}234,56", PriceContext(), "1234.56"),
    (f"1{NNBSP}234,56 €", PriceContext(), "1234.56"),
    (f"1{THIN}234.56", PriceContext(), "1234.56"),
    ("1'234.56", PriceContext(), "1234.56"),
    (f"1{RSQUO}234.56", PriceContext(), "1234.56"),
    ("1 234 567", PriceContext(), "1234567"),
    ("1,5", PriceContext(), "1.5"),
    ("19,-", PriceContext(), "19"),
    (f"19,{EN_DASH}", PriceContext(), "19"),
    ("1.234,-", PriceContext(), "1234"),
    ("12,345円", PriceContext(currency="JPY"), "12345"),
    ("₹1,23,456", PriceContext(currency="INR"), "123456"),
    ("₹1,23,456.50", PriceContext(currency="INR"), "123456.50"),
    ("1,23,456", PriceContext(grouping="indian"), "123456"),
    (_deva("1234"), PriceContext(digits="deva"), "1234"),
    (_arab("1234"), PriceContext(digits="arab"), "1234"),
    (_arab("1234") + ARABIC_DECIMAL + _arab("56"), PriceContext(digits="arab"), "1234.56"),
    (_arab("1") + ARABIC_GROUPING + _arab("234"), PriceContext(digits="arab"), "1234"),
    (
        _fullwidth("1") + FW_COMMA + _fullwidth("234"),
        PriceContext(currency="JPY", digits="fullwidth"),
        "1234",
    ),
    ("£1,234.56", PriceContext(), "1234.56"),
    ("¥1,234", PriceContext(currency="JPY"), "1234"),
    ("12,345원", PriceContext(currency="KRW"), "12345"),
    ("BHD 1.234", PriceContext(), "1.234"),
    ("KWD 12.345", PriceContext(), "12.345"),
    ("1 234.567", PriceContext(currency="BHD"), "1234.567"),
    ("12.345", PriceContext(currency="BHD"), "12.345"),
    ("12,345", PriceContext(currency="KWD"), "12.345"),
    ("R$ 1.234,56", PriceContext(), "1234.56"),
    ("1 234,56 zł", PriceContext(), "1234.56"),
    ("1.234,56 kr", PriceContext(currency="NOK"), "1234.56"),
    ("EUR 12,50", PriceContext(), "12.50"),
    ("12.50 USD", PriceContext(), "12.50"),
    ("1234.567", PriceContext(), "1234.567"),
    ("1000000000", PriceContext(), "1000000000"),
]


@pytest.mark.parametrize(("text", "ctx", "expected"), ACCEPT)
def test_acceptance_table(text: str, ctx: PriceContext, expected: str) -> None:
    assert parse_price_text(text, ctx) == Decimal(expected)


REJECT: list[tuple[str, PriceContext]] = [
    # the rejection table of the specification
    ("1e3", PriceContext()),
    ("-5", PriceContext()),
    (f"{MINUS}5", PriceContext()),
    ("+5", PriceContext()),
    ("0", PriceContext()),
    ("0,00", PriceContext()),
    ("NaN", PriceContext()),
    ("Infinity", PriceContext()),
    ("", PriceContext()),
    ("12 34", PriceContext()),
    ("1,23,45", PriceContext()),
    ("1,23,45", PriceContext(currency="INR")),
    ("12,", PriceContext()),
    ("€ -5", PriceContext()),
    (f"EUR 10{EN_DASH}20", PriceContext()),
    ("Was 20 now 10", PriceContext()),
    ("10 €/kg", PriceContext()),
    ("€ 10 USD", PriceContext()),
    ("1,2345", PriceContext()),
    ("1.234.567", PriceContext(currency="BHD")),
    ("1,234.567", PriceContext(currency="BHD")),
    ("¥12.50", PriceContext(currency="JPY")),
    ("¥12.5", PriceContext(currency="JPY")),
    (_arab("1234"), PriceContext()),
    ("1,23,456", PriceContext(currency="EUR")),
    ("1'234,567.89", PriceContext()),
    ("1" * 65, PriceContext()),
    ("1" * 16, PriceContext()),
    # separators: mixed, doubled, leading, trailing, script separators without script
    (f"1 234{NBSP}567", PriceContext()),
    ("1..234", PriceContext()),
    (",99", PriceContext()),
    ("1.234,567,890", PriceContext()),
    (_arab("1") + ARABIC_DECIMAL + _arab("5"), PriceContext()),
    ("1" + FW_COMMA + "234", PriceContext(currency="JPY")),
    # mixed digit scripts in one number
    ("1" + _arab("234"), PriceContext(digits="arab")),
    # precision: fraction longer than the currency allows
    ("1.5", PriceContext(currency="JPY")),
    ("1,234.567", PriceContext(currency="EUR")),
    # above the bound
    ("1000000001", PriceContext()),
]


@pytest.mark.parametrize(("text", "ctx"), REJECT)
def test_rejection_table(text: str, ctx: PriceContext) -> None:
    assert parse_price_text(text, ctx) is None


def test_single_separator_three_digits_groups() -> None:
    """Under a currency with at most two decimals, '.'+3 digits is a thousands group."""
    assert parse_price_text("12.345", PriceContext(currency="EUR")) == Decimal("12345")
    assert parse_price_text("12,345", PriceContext(currency="USD")) == Decimal("12345")
    assert parse_price_text("12.345", PriceContext(currency="JPY")) == Decimal("12345")


def test_single_separator_three_digits_without_currency_is_ambiguous() -> None:
    """Thousands or decimals cannot be told apart without a currency: rejected."""
    for text in ("12.345", "12,345", "1.234", "999,999", "$12.345", "kr 1.234", "¥1,234"):
        assert parse_price_text(text) is None, text
    # the same strings are readable once the context fixes the precision
    assert parse_price_text("12.345", PriceContext(currency="BHD")) == Decimal("12.345")
    assert parse_price_text("kr 1.234", PriceContext(currency="SEK")) == Decimal("1234")


def test_negative_rejected() -> None:
    for text in ("-5", f"{MINUS}5", "+5", "€ -5", "-12,50 €", "5-", "(5)"):
        assert parse_price_text(text) is None, text
    assert parse_price_text("5") == Decimal("5")


def test_currency_token_contradicting_context_is_rejected() -> None:
    assert parse_price_text("USD 10", PriceContext(currency="EUR")) is None
    assert parse_price_text("₹10", PriceContext(currency="EUR")) is None
    assert parse_price_text("kr 10", PriceContext(currency="EUR")) is None
    assert parse_price_text("kr 10", PriceContext(currency="NOK")) == Decimal("10")


def test_symbol_glued_to_a_word_is_not_a_token() -> None:
    assert parse_price_text("krone 10") is None
    assert parse_price_text("EURO 10") is None
    assert parse_price_text("EUR10") == Decimal("10")


# ------------------------------------------------ structured strings


def test_structured_price_with_excess_precision_is_rejected() -> None:
    """``price="12.345", priceCurrency="EUR"`` is not twelve thousand euro."""
    offer = {"price": "12.345", "priceCurrency": "EUR"}
    assert select_offer(offer) == Unreadable("malformed")
    assert parse_price_value("12.345", PriceContext(currency="EUR"), structured=True) is None
    # the visible text "12.345" with EUR is a thousands group; the structured one is not
    assert parse_price_text("12.345", PriceContext(currency="EUR")) == Decimal("12345")


def test_structured_grammar_has_one_separator() -> None:
    ctx = PriceContext(currency="EUR")
    assert parse_price_value("29.99", ctx, structured=True) == Decimal("29.99")
    assert parse_price_value("1234", ctx, structured=True) == Decimal("1234")
    for text in ("29,99", "1,234.56", "1.234,56", "€29.99", "29.99 EUR", "1 234", "29.9.9"):
        assert parse_price_value(text, ctx, structured=True) is None, text
    assert parse_price_value("12.345", PriceContext(currency="BHD"), structured=True) == Decimal(
        "12.345"
    )
    assert parse_price_value("12.345", PriceContext(), structured=True) is None
    assert parse_price_value("1234.567", PriceContext(), structured=True) == Decimal("1234.567")


def test_json_number_is_not_the_string_1e3() -> None:
    assert decode_price_value("1e3") == PriceText("1e3", PriceKind.VISUAL)
    assert parse_price_value("1e3") is None
    assert parse_price_value(Decimal("1E+3")) == Decimal("1000")
    assert parse_price_value(1e3) == Decimal("1000")
    assert parse_price_value(Decimal("12.345"), PriceContext(currency="EUR")) is None
    assert parse_price_value(Decimal("12.340"), PriceContext(currency="EUR")) == Decimal("12.34")


# ------------------------------------------------------------------ scale


def test_data_price_2999_next_to_29_99_is_never_scaled() -> None:
    """A visible or attribute text is read as it is written; no scale is inferred."""
    assert parse_price_text("2999") == Decimal("2999")
    assert parse_price_text("€29,99") == Decimal("29.99")
    minor = PriceContext(currency="EUR", unit="minor")
    assert parse_price_text("2999", minor) is None  # an HTML reader never sets a unit
    assert parse_price_value(2999, minor) == Decimal("29.99")  # a documented API does
    assert parse_price_value(2999, PriceContext(unit="minor")) is None  # scale needs currency
    assert parse_price_value(Decimal("2999.5"), minor) is None
    assert parse_price_value(2999, PriceContext(currency="JPY", unit="minor")) == Decimal("2999")
    assert parse_price_value(2999, PriceContext(currency="BHD", unit="minor")) == Decimal("2.999")


# ------------------------------------------------------------------ typed decoding

DECODE_NONE: list[object] = [
    True,
    False,
    None,
    [100],
    {"amount": 100},
    (100,),
    b"100",
    0,
    -5,
    10**12,
    0.0,
    -1.5,
    float("nan"),
    float("inf"),
    float("-inf"),
    1e12,
    Decimal("NaN"),
    Decimal("sNaN"),
    Decimal("Infinity"),
    Decimal("-3"),
    Decimal(0),
    "",
    "   ",
    "1" * 65,
    object(),
]


@pytest.mark.parametrize("value", DECODE_NONE, ids=repr)
def test_decode_rejects_non_prices(value: object) -> None:
    assert decode_price_value(value) is None
    assert decode_price_value(value, structured=True) is None
    assert parse_price_value(value) is None


def test_decode_numbers_and_strings() -> None:
    assert decode_price_value(2999) == PriceText("2999", PriceKind.NUMERIC_LITERAL)
    assert decode_price_value(29.99) == PriceText("29.99", PriceKind.NUMERIC_LITERAL)
    assert decode_price_value(Decimal("29.99")) == PriceText("29.99", PriceKind.NUMERIC_LITERAL)
    assert decode_price_value(" 29,99 € ") == PriceText("29,99 €", PriceKind.VISUAL)
    assert decode_price_value("29.99", structured=True) == PriceText("29.99", PriceKind.STRUCTURED)


def test_float_representation_noise_is_rejected() -> None:
    assert parse_price_value(0.1 + 0.2) is None  # 0.30000000000000004
    assert parse_price_value(1e-7) is None  # repr is exponent notation


def test_context_validation() -> None:
    with pytest.raises(ValueError, match="not accepted"):
        PriceContext(currency="XTS")
    with pytest.raises(ValueError, match="not accepted"):
        PriceContext(currency="eur")
    with pytest.raises(ValueError, match="digit script"):
        PriceContext(digits="roman")
    with pytest.raises(ValueError, match="grouping"):
        PriceContext(grouping="chinese")
    with pytest.raises(ValueError, match="unit"):
        PriceContext(unit="cents")


def test_parse_rejects_non_text_inputs() -> None:
    assert parse_price_text(PriceText("12", PriceKind.VISUAL)) == Decimal("12")
    assert parse_price_text(123) is None  # type: ignore[arg-type]
    assert parse_price_text(PriceText(123, PriceKind.VISUAL)) is None  # type: ignore[arg-type]
    assert parse_price_text(PriceText("12", "visual")) is None  # type: ignore[arg-type]


# ------------------------------------------------------------------ offer selection


def test_offer_financing_only() -> None:
    offers = [
        {
            "price": "49.99",
            "priceCurrency": "EUR",
            "priceSpecification": {"@type": "PaymentChargeSpecification"},
        },
        {"price": "19.99", "priceCurrency": "EUR", "description": "19.99 per month"},
    ]
    assert select_offer(offers) == Unreadable("financing_only")


def test_offer_range_and_strikethrough_are_never_candidates() -> None:
    aggregate = {"@type": "AggregateOffer", "lowPrice": "10", "highPrice": "20", "price": "10"}
    assert select_offer(aggregate) == Unreadable("range")
    strike = {
        "price": "99",
        "priceCurrency": "EUR",
        "priceSpecification": {"priceType": "https://schema.org/StrikethroughPrice"},
    }
    buy = {"price": "79", "priceCurrency": "EUR"}
    assert select_offer([strike, buy]) == Money(Decimal("79"), "EUR")
    assert select_offer([strike]) == Unreadable("malformed")
    unit = {
        "price": "2.50",
        "priceCurrency": "EUR",
        "priceSpecification": {"referenceQuantity": {"value": 1, "unitCode": "KGM"}},
    }
    assert select_offer([unit, buy]) == Money(Decimal("79"), "EUR")


def test_offer_multi_currency_tie_is_unreadable_in_every_order() -> None:
    eur = {"price": 100, "priceCurrency": "EUR"}
    usd = {"price": 100, "priceCurrency": "USD"}
    assert select_offer([eur, usd]) == Unreadable("currencies_disagree")
    assert select_offer([usd, eur]) == Unreadable("currencies_disagree")


def test_offer_highest_candidate_wins() -> None:
    offers = [{"price": "29.99"}, {"price": "39.99", "priceCurrency": "eur"}, {"price": "9.99"}]
    assert select_offer(offers) == Money(Decimal("39.99"), "EUR")


def test_offer_malformed_shapes() -> None:
    assert select_offer({"price": {"amount": 100}}) == Unreadable("malformed")
    assert select_offer({"price": [100]}) == Unreadable("malformed")
    assert select_offer({"price": True}) == Unreadable("malformed")
    assert select_offer("29.99") == Unreadable("malformed")
    assert select_offer(None) == Unreadable("no_offer")
    assert select_offer([]) == Unreadable("no_offer")
    assert select_offer([None, 3]) == Unreadable("malformed")
    assert select_offer({"price": "10", "priceCurrency": "€"}) == Unreadable("malformed")
    assert select_offer({"price": "10", "priceCurrency": "XTS"}) == Unreadable("malformed")
    assert select_offer({"price": "10", "priceSpecification": [3]}) == Unreadable("malformed")
    # a currency-less offer more precise than the node's currency makes the node unreadable
    offers = [{"price": "1234.567"}, {"price": "10", "priceCurrency": "EUR"}]
    assert select_offer(offers) == Unreadable("malformed")


def test_offer_currency_contradicting_context() -> None:
    offer = {"price": "10", "priceCurrency": "USD"}
    assert select_offer(offer, PriceContext(currency="EUR")) == Unreadable("malformed")
    assert select_offer({"price": "10"}, PriceContext(currency="EUR")) == Money(
        Decimal("10"), "EUR"
    )


def test_unreadable_reason_is_closed() -> None:
    with pytest.raises(ValueError, match="unknown unreadable reason"):
        Unreadable("because")


_f6_a = "https://shop.example.com/p/A"
_f6_b = "https://shop.example.com/p/B"
_f6_requested = RequestedIdentity.from_url(_f6_a)


def _f6_product(**fields: object) -> dict[str, object]:
    return {"@type": "Product", "offers": {"price": "9.99", "priceCurrency": "EUR"}, **fields}


def _f6_anchor(doc: object):
    return anchor_structured_page([json.dumps(doc)], _f6_requested)


@settings(max_examples=60, database=None)
@given(
    st.integers(min_value=1, max_value=10000),
    st.sampled_from(
        [
            {"@type": "PaymentChargeSpecification"},
            {"billingDuration": "P12M"},
            {"billingIncrement": 1},
            {"referenceQuantity": {"value": 1}},
            {"name": "12 monthly payments"},
            {"description": "Pay in 4 installments"},
        ]
    ),
)
def test_excluded_offer_context_never_produces_price(amount, context):
    full = {"price": str(amount + 100), "priceCurrency": "EUR"}
    excluded = {"price": str(amount), "priceCurrency": "EUR", **context}
    assert isinstance(select_offer(excluded), Unreadable)
    for offers in ([excluded, full], [full, excluded]):
        assert select_offer(offers) == Money(Decimal(amount + 100), "EUR")
    node = _f6_product(url=_f6_a)
    node["offers"] = excluded
    assert _f6_anchor(node).reason == "requested_unreadable"


@pytest.mark.parametrize("field", ["billingDuration", "billingIncrement"])
@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
def test_malformed_billing_field_does_not_become_a_cash_price(field, value):
    offer = {"price": "9.99", "priceCurrency": "EUR", "priceSpecification": {field: value}}
    assert isinstance(select_offer(offer), Unreadable)
