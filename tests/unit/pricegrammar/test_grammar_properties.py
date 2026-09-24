"""Property tests of the price grammar.

The generators build strings **from a known Decimal** and a formatting convention, and
compute the expected outcome from the construction alone — never by calling the
parser: a valid combination must round-trip to the same Decimal, a combination that is
invalid or ambiguous by construction must be rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from price_tracker.core.money import MAX_AMOUNT, Money
from price_tracker.core.pricegrammar import (
    PriceContext,
    Unreadable,
    decode_price_value,
    parse_price_text,
    parse_price_value,
    select_offer,
)

NBSP = chr(0x00A0)
NNBSP = chr(0x202F)
THIN = chr(0x2009)
RSQUO = chr(0x2019)
EN_DASH = chr(0x2013)
MINUS = chr(0x2212)
ARABIC_DECIMAL = chr(0x066B)
ARABIC_GROUPING = chr(0x066C)
DUAL = frozenset({".", ","})

PRECISION = {"EUR": 2, "USD": 2, "INR": 2, "JPY": 0, "KRW": 0, "BHD": 3, "KWD": 3}
# (grouping character or None, decimal character)
LATIN_CONVENTIONS = [
    (".", ","),
    (",", "."),
    (" ", ","),
    (NBSP, ","),
    (NNBSP, ","),
    (THIN, "."),
    ("'", "."),
    (RSQUO, "."),
    (None, "."),
    (None, ","),
]
SCRIPT_ZERO = {"arab": 0x0660, "arabext": 0x06F0, "fullwidth": 0xFF10, "deva": 0x0966}
FULLWIDTH_SEPARATORS = {",": chr(0xFF0C), ".": chr(0xFF0E)}


@dataclass(frozen=True)
class Case:
    text: str
    ctx: PriceContext
    amount: Decimal
    expected: Decimal | None  # derived from the construction only
    has_token: bool


def _group(digits: str, style: str) -> list[str]:
    if style == "none" or len(digits) <= 3:
        return [digits]
    if style == "western":
        head = len(digits) % 3 or 3
        return [digits[:head]] + [digits[i : i + 3] for i in range(head, len(digits), 3)]
    tail = digits[-3:]
    rest = digits[:-3]
    head = len(rest) % 2 or 2
    return [rest[:head]] + [rest[i : i + 2] for i in range(head, len(rest), 2)] + [tail]


@st.composite
def formatted_prices(draw: st.DrawFn) -> Case:
    currency = draw(st.sampled_from([None, *PRECISION]))
    precision = PRECISION.get(currency) if currency else None
    context_given = draw(st.booleans())
    token = currency is not None and draw(st.booleans())
    if currency is not None and not token:
        context_given = True
    known_precision = precision if currency is not None else None
    max_fraction = 3 if known_precision is None else known_precision

    integer = draw(st.integers(min_value=0, max_value=999_999_999))
    fraction_len = draw(st.integers(min_value=0, max_value=max_fraction))
    fraction = "".join(
        draw(st.lists(st.sampled_from("0123456789"), min_size=fraction_len, max_size=fraction_len))
    )
    amount = Decimal(f"{integer}.{fraction}") if fraction else Decimal(integer)
    assume(0 < amount <= MAX_AMOUNT)

    script = draw(st.sampled_from(["latin", "latin", "arab", "fullwidth", "deva"]))
    if script == "arab":
        group_char, decimal_char = draw(
            st.sampled_from([(ARABIC_GROUPING, ARABIC_DECIMAL), (None, ARABIC_DECIMAL)])
        )
        style = "western" if group_char else "none"
    else:
        indian = currency == "INR" and draw(st.booleans())
        if indian:
            group_char, decimal_char, style = ",", ".", "indian"
        else:
            group_char, decimal_char = draw(st.sampled_from(LATIN_CONVENTIONS))
            style = "western" if group_char else "none"

    groups = _group(str(integer), style)
    group_seps = len(groups) - 1
    body = (group_char or "").join(groups)
    if fraction:
        body += decimal_char + fraction

    # expected outcome, from the construction
    expected: Decimal | None = amount
    separators = group_seps + (1 if fraction else 0)
    lone_dual = separators == 1 and (
        (group_seps == 1 and group_char in DUAL) or (fraction and decimal_char in DUAL)
    )
    if lone_dual and known_precision is None:
        after = fraction if fraction else groups[-1]
        before = str(integer) if fraction else groups[0]
        if len(after) == 3 and 1 <= len(before) <= 3:
            expected = None  # thousands or decimals: ambiguous by construction
    if known_precision == 3 and group_seps and group_char in DUAL:
        # A three-decimal currency never groups with '.' or ','. With one such
        # separator and no fraction the string is a valid three-decimal number (the
        # "1.000" of 1 BHD); with more it is not a number at all.
        single = group_seps == 1 and not fraction
        expected = Decimal(f"{groups[0]}.{groups[1]}") if single else None

    if script == "fullwidth":
        body = "".join(FULLWIDTH_SEPARATORS.get(c, c) for c in body)
    if script != "latin":
        zero = SCRIPT_ZERO[script]
        body = "".join(chr(zero + int(c)) if "0" <= c <= "9" else c for c in body)

    text = body
    if token and currency is not None:
        text = draw(st.sampled_from([f"{currency} {body}", f"{body} {currency}", currency + body]))
    ctx = PriceContext(currency=currency if context_given else None, digits=script)
    return Case(text, ctx, amount, expected, token)


@settings(max_examples=1500, deadline=None)
@given(formatted_prices())
def test_formatted_price_round_trips_or_is_rejected(case: Case) -> None:
    assert parse_price_text(case.text, case.ctx) == case.expected


@settings(max_examples=300, deadline=None)
@given(formatted_prices())
def test_undeclared_non_latin_digits_are_rejected(case: Case) -> None:
    assume(case.ctx.digits != "latin")
    latin_ctx = PriceContext(currency=case.ctx.currency)
    assert parse_price_text(case.text, latin_ctx) is None


def _hostile(text: str, kind: str) -> str:
    decorations = {
        "minus": "-" + text,
        "unicode_minus": MINUS + text,
        "plus": "+" + text,
        "second_number": text + " 10",
        "range": text + EN_DASH + "20",
        "unit": text + "/kg",
        "words": "Was " + text,
        "second_token": "€ " + text + " USD",
        "exponent": text + "e3",
        "length": "0" * max(0, 65 - len(text)) + text,
        "trailing_separator": text + ",",
        "trailing_dot": text + ".",
        "percent": text + "%",
        "parenthesis": "(" + text + ")",
    }
    return decorations[kind]


HOSTILE_KINDS = [
    "minus",
    "unicode_minus",
    "plus",
    "second_number",
    "range",
    "unit",
    "words",
    "second_token",
    "exponent",
    "length",
    "trailing_separator",
    "trailing_dot",
    "percent",
    "parenthesis",
]


@settings(max_examples=1000, deadline=None)
@given(formatted_prices(), st.sampled_from(HOSTILE_KINDS))
def test_hostile_decorations_are_rejected(case: Case, kind: str) -> None:
    assume(case.expected is not None)
    assert parse_price_text(case.text, case.ctx) == case.expected  # control: valid as built
    assert parse_price_text(_hostile(case.text, kind), case.ctx) is None


@settings(max_examples=500, deadline=None)
@given(formatted_prices(), st.data())
def test_foreign_digit_injected_is_rejected(case: Case, data: st.DataObject) -> None:
    assume(case.ctx.digits == "latin" and case.expected is not None)
    positions = [i for i, c in enumerate(case.text) if "0" <= c <= "9"]
    index = data.draw(st.sampled_from(positions))
    foreign = data.draw(st.characters(categories=["Nd"]).filter(lambda c: not "0" <= c <= "9"))
    mutated = case.text[:index] + foreign + case.text[index + 1 :]
    assert parse_price_text(mutated, case.ctx) is None


GROUP_POOL = [".", ",", " ", NBSP, NNBSP, THIN, "'", RSQUO]


@settings(max_examples=500, deadline=None)
@given(
    st.integers(min_value=1_000_000, max_value=999_999_999),
    st.lists(st.sampled_from(GROUP_POOL), min_size=2, max_size=2, unique=True),
    st.sampled_from(["", ",5", ".5"]),
    st.sampled_from([None, "EUR", "JPY"]),
)
def test_mixed_grouping_is_rejected(
    integer: int, pair: list[str], fraction: str, currency: str | None
) -> None:
    """Two different grouping characters in one number: never a price."""
    first, other = pair
    assume(not fraction or fraction[0] not in pair)
    # without a fraction, a dual character in last position is a decimal point, and
    # the string is then a valid three-decimal number, not a mixed grouping
    assume(fraction or other not in DUAL)
    groups = _group(str(integer), "western")
    uniform = first.join(groups) + fraction
    mixed = first.join(groups[:-1]) + other + groups[-1] + fraction
    ctx = PriceContext(currency=currency)
    expected_uniform = Decimal(str(integer) + (("." + fraction[1:]) if fraction else ""))
    if not (currency == "JPY" and fraction):
        assert parse_price_text(uniform, ctx) == expected_uniform  # control
    assert parse_price_text(mixed, ctx) is None


# ------------------------------------------------------------------ totality

json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**20), max_value=10**20)
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.decimals(allow_nan=True, allow_infinity=True)
    | st.text(max_size=80)
    | st.binary(max_size=8)
)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)
contexts = st.builds(
    PriceContext,
    currency=st.sampled_from([None, "EUR", "JPY", "BHD", "INR", "USD"]),
    digits=st.sampled_from(["latin", "arab", "arabext", "fullwidth", "deva"]),
    grouping=st.sampled_from(["auto", "western", "indian"]),
    unit=st.sampled_from(["major", "minor"]),
)


def _valid_amount(value: Decimal | None) -> bool:
    return value is None or (value.is_finite() and 0 < value <= MAX_AMOUNT)


@settings(max_examples=1500, deadline=None)
@given(json_values, contexts, st.booleans())
def test_decode_and_parse_never_raise_and_stay_in_bounds(
    value: object, ctx: PriceContext, structured: bool
) -> None:
    decoded = decode_price_value(value, structured=structured)
    if decoded is not None:
        assert 1 <= len(decoded.text) <= 64
    assert _valid_amount(parse_price_value(value, ctx, structured=structured))


@settings(max_examples=1500, deadline=None)
@given(st.text(max_size=80), contexts)
def test_arbitrary_text_never_raises(text: str, ctx: PriceContext) -> None:
    assert _valid_amount(parse_price_text(text, ctx))


@settings(max_examples=800, deadline=None)
@given(json_values, contexts)
def test_select_offer_is_total(offers: object, ctx: PriceContext) -> None:
    result = select_offer(offers, ctx)
    assert isinstance(result, Money | Unreadable)


# ------------------------------------------------------------------ offers: order

OFFER_POOL = [
    {"price": "29.99", "priceCurrency": "EUR"},
    {"price": "39.99", "priceCurrency": "EUR"},
    {"price": 19, "priceCurrency": "USD"},
    {"price": "24.50"},
    {"price": "9.99", "priceSpecification": {"@type": "PaymentChargeSpecification"}},
    {"@type": "AggregateOffer", "lowPrice": "5", "highPrice": "50"},
    {"price": "99", "priceSpecification": {"priceType": "https://schema.org/ListPrice"}},
    {"price": "2.50", "priceSpecification": {"referenceQuantity": {"value": 1}}},
    {"price": {"amount": 100}},
    {"price": True},
    "not an offer",
]


@settings(max_examples=600, deadline=None)
@given(
    st.lists(st.sampled_from(range(len(OFFER_POOL))), min_size=1, max_size=6).flatmap(
        lambda idx: st.tuples(st.just(idx), st.permutations(idx))
    )
)
def test_offer_selection_is_order_independent(pair: tuple[list[int], list[int]]) -> None:
    original, permuted = pair
    first = select_offer([OFFER_POOL[i] for i in original])
    second = select_offer([OFFER_POOL[i] for i in permuted])
    assert first == second
    if isinstance(first, Money):
        assert first.currency in {None, "EUR", "USD"}


@settings(max_examples=100, database=None)
@given(st.integers(min_value=1, max_value=999), st.sampled_from(["latin", "deva", "fullwidth"]))
def test_arabic_separators_require_declared_script(integer, script):
    for suffix, expected in [
        ("\u066b25", Decimal(integer) + Decimal(".25")),
        ("\u066c234", Decimal(integer * 1000 + 234)),
    ]:
        text = str(integer) + suffix
        assert parse_price_value(text, PriceContext(currency="EUR", digits=script)) is None
        for declared in ["arab", "arabext"]:
            assert (
                parse_price_value(text, PriceContext(currency="EUR", digits=declared)) == expected
            )
