"""Not-well-formed inputs: payloads, fields, observations and resolver states.

Every rejection is paired with a positive control on the same path, and asserts the
exact state and reason — a ``price is None`` reached through an unrelated failure proves
nothing.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.anchoring import (
    MAX_OBSERVATIONS,
    REASONS_BY_STATE,
    AnchorResult,
    AnchorState,
    Money,
    Observation,
    Ownership,
    resolve_anchor,
)
from price_tracker.core.identity import RequestedIdentity, normalize_url
from price_tracker.core.pricegrammar import decode_price_value
from price_tracker.core.structured_data import (
    MAX_DEPTH,
    MAX_PAYLOAD_BYTES,
    StructureError,
    anchor_structured_page,
    decode_json_strict,
)

from ._docgen import (
    ITEMLIST,
    REQUESTED_URL,
    NodeSpec,
    PageSpec,
    count_objects,
    dumps,
    render,
)
from .test_resolver_properties import pages

REQUESTED = RequestedIdentity.from_url(REQUESTED_URL)
EUR = "EUR"
TERMINAL = (AnchorState.AMBIGUOUS, None, "malformed_structure")


def _outcome(result: AnchorResult) -> tuple[AnchorState, Money | None, str]:
    return result.state, result.price, result.reason


def _base_page() -> PageSpec:
    return PageSpec(
        nodes=(
            NodeSpec(0, Decimal("89.00"), True),
            NodeSpec(1, Decimal("24.90"), False, context=ITEMLIST),
        )
    )


def _good_payload() -> dict[str, object]:
    return {
        "@context": "https://schema.org",
        "@type": "Product",
        "url": REQUESTED_URL,
        "offers": {"@type": "Offer", "price": "89.00", "priceCurrency": "EUR"},
    }


def test_control_payload_is_found() -> None:
    result = anchor_structured_page([json.dumps(_good_payload())], REQUESTED)
    assert _outcome(result) == (AnchorState.FOUND, Money(Decimal("89.00"), EUR), "owned")


# ------------------------------------------------------------------ duplicate keys at any depth


@settings(max_examples=800, deadline=None)
@given(pages(), st.data())
def test_duplicate_key_at_any_depth_is_terminal(page: PageSpec, data: st.DataObject) -> None:
    rendered = render(page)
    if not rendered.documents:
        return
    index = data.draw(st.integers(min_value=0, max_value=len(rendered.documents) - 1))
    document = rendered.documents[index]
    target = data.draw(st.integers(min_value=0, max_value=count_objects(document) - 1))
    conflicting = data.draw(st.booleans())
    first = data.draw(st.booleans())
    seed = data.draw(st.integers(min_value=0, max_value=20))
    payloads = list(rendered.payloads)
    payloads[index] = dumps(
        document,
        seed=seed,
        duplicate_at=target,
        duplicate_value="https://shop.example.com/p/conflict" if conflicting else None,
        duplicate_first=first,
    )
    result = anchor_structured_page(payloads, REQUESTED, extra=rendered.extra)
    assert _outcome(result) == TERMINAL
    # control: the same document without the duplicate, same key order, same path
    payloads[index] = dumps(document, seed=seed)
    control = anchor_structured_page(payloads, REQUESTED, extra=rendered.extra)
    assert control.reason != "malformed_structure"


def test_duplicate_key_cannot_be_rescued_by_other_sources() -> None:
    page = _base_page()
    rendered = render(page)
    dup = dumps(rendered.documents[0], duplicate_at=0)
    trusted = Observation(
        "container:#buybox", Ownership.TRUSTED, "c", None, Money(Decimal("89.00"), EUR)
    )
    payloads = [dup, *rendered.payloads[1:]]
    assert _outcome(anchor_structured_page(payloads, REQUESTED, extra=(trusted,))) == TERMINAL


# ------------------------------------------------------------------ truncation, literals


@settings(max_examples=500, deadline=None)
@given(pages(), st.data())
def test_truncated_payload_is_terminal(page: PageSpec, data: st.DataObject) -> None:
    rendered = render(page)
    if not rendered.payloads:
        return
    index = data.draw(st.integers(min_value=0, max_value=len(rendered.payloads) - 1))
    text = rendered.payloads[index]
    cut = data.draw(st.integers(min_value=1, max_value=len(text) - 1))
    payloads = list(rendered.payloads)
    payloads[index] = text[:cut]
    assert _outcome(anchor_structured_page(payloads, REQUESTED, extra=rendered.extra)) == TERMINAL


GOOD = json.dumps(_good_payload())
MALFORMED_TEXTS = [
    GOOD.replace('"89.00"', "NaN"),
    GOOD.replace('"89.00"', "Infinity"),
    GOOD.replace('"89.00"', "-Infinity"),
    chr(0xFEFF) + GOOD,
    "// product\n" + GOOD,
    GOOD[:-1] + ",}",
    GOOD.replace('"', "&quot;"),
    GOOD.replace('"price": "89.00"', '"price": 89.00,'),
    "",
    "   ",
    "{" * (MAX_DEPTH + 1) + "}" * (MAX_DEPTH + 1),
    '{"a":' * 10_000 + "1" + "}" * 10_000,
    "[" * 10_000 + "]" * 10_000,
    GOOD.replace('"89.00"', "1" + "0" * 5000),  # integer beyond the digit limit
]


@pytest.mark.parametrize("text", MALFORMED_TEXTS, ids=lambda t: t[:24])
def test_malformed_payload_is_terminal(text: str) -> None:
    assert _outcome(anchor_structured_page([text, GOOD], REQUESTED)) == TERMINAL
    assert anchor_structured_page([GOOD], REQUESTED).state is AnchorState.FOUND


def test_depth_limit_boundary() -> None:
    at_limit = '{"a":' * (MAX_DEPTH - 1) + "{}" + "}" * (MAX_DEPTH - 1)
    assert decode_json_strict(at_limit) is not None
    over = '{"a":' * MAX_DEPTH + "{}" + "}" * MAX_DEPTH
    with pytest.raises(StructureError, match="nested deeper"):
        decode_json_strict(over)
    # brackets inside strings do not count
    assert decode_json_strict(json.dumps({"s": "{" * 500})) == {"s": "{" * 500}


def test_size_limit_boundary() -> None:
    filler = MAX_PAYLOAD_BYTES - len(GOOD) - len(', "description": ""') - 8
    under = GOOD[:-1] + ', "description": "' + "x" * filler + '"}'
    assert anchor_structured_page([under], REQUESTED).state is AnchorState.FOUND
    over = GOOD[:-1] + ', "description": "' + "x" * MAX_PAYLOAD_BYTES + '"}'
    assert _outcome(anchor_structured_page([over], REQUESTED)) == TERMINAL


def test_json_numbers_are_decimals_never_floats() -> None:
    decoded = decode_json_strict('{"price": 12.345, "n": 3}')
    assert decoded == {"price": Decimal("12.345"), "n": 3}
    huge = GOOD.replace('"89.00"', "1e400")
    result = anchor_structured_page([huge], REQUESTED)
    assert _outcome(result) == (AnchorState.NO_CANDIDATE, None, "requested_unreadable")


# ------------------------------------------------------------------ wrong types, fields

IDENTITY_BREAKS = [
    ("url", 123),
    ("url", {"href": REQUESTED_URL}),
    ("url", [REQUESTED_URL, 7]),
    ("@id", 5),
    ("@type", ["Product", None]),
    ("@type", 7),
    ("@type", ""),
    ("sku", {"v": 1}),
    ("sku", True),
]


@pytest.mark.parametrize(("field", "value"), IDENTITY_BREAKS, ids=repr)
def test_wrong_typed_identity_field_is_terminal(field: str, value: object) -> None:
    payload = _good_payload()
    payload[field] = value
    assert _outcome(anchor_structured_page([json.dumps(payload)], REQUESTED)) == TERMINAL


PRICE_BREAKS = [
    ("offers", "89.00"),
    ("offers", 89),
    ("offers", [None]),
    ("offers", {"price": True, "priceCurrency": "EUR"}),
    ("offers", {"price": [89], "priceCurrency": "EUR"}),
    ("offers", {"price": {"amount": 89}, "priceCurrency": "EUR"}),
    ("offers", {"price": "89.00", "priceCurrency": 978}),
    ("offers", {"price": "89.00", "priceCurrency": "€"}),
    ("offers", {"price": "", "priceCurrency": "EUR"}),
    ("offers", {"price": "-89", "priceCurrency": "EUR"}),
    ("offers", {"price": "89.001", "priceCurrency": "EUR"}),
    ("offers", []),
    ("offers", None),
]


@pytest.mark.parametrize(("field", "value"), PRICE_BREAKS, ids=repr)
def test_wrong_typed_price_field_is_requested_unreadable(field: str, value: object) -> None:
    payload = _good_payload()
    payload[field] = value
    carousel = json.dumps(
        {
            "@type": "ItemList",
            "itemListElement": [
                {
                    "@type": "Product",
                    "url": "https://shop.example.com/p/other",
                    "offers": {"price": "24.90", "priceCurrency": "EUR"},
                }
            ],
        }
    )
    result = anchor_structured_page([json.dumps(payload), carousel], REQUESTED)
    assert _outcome(result) == (AnchorState.NO_CANDIDATE, None, "requested_unreadable")


def test_missing_offers_is_requested_unreadable() -> None:
    payload = _good_payload()
    del payload["offers"]
    result = anchor_structured_page([json.dumps(payload)], REQUESTED)
    assert _outcome(result) == (AnchorState.NO_CANDIDATE, None, "requested_unreadable")
    assert result.deciding[0].unreadable_reason == "no_offer"


@settings(max_examples=500, deadline=None)
@given(
    pages(),
    st.dictionaries(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=8).map(
            lambda s: "x" + s
        ),
        st.none() | st.booleans() | st.integers() | st.text(max_size=10),
        min_size=1,
        max_size=3,
    ),
)
def test_unknown_extra_fields_change_nothing(page: PageSpec, extras: dict[str, object]) -> None:
    rendered = render(page)

    def decorate(value: object) -> object:
        if isinstance(value, dict):
            return {**{k: decorate(v) for k, v in value.items()}, **extras}
        if isinstance(value, list):
            return [decorate(v) for v in value]
        return value

    decorated = [json.dumps(decorate(doc)) for doc in rendered.documents]
    baseline = anchor_structured_page(rendered.payloads, REQUESTED, extra=rendered.extra)
    result = anchor_structured_page(decorated, REQUESTED, extra=rendered.extra)
    assert _outcome(result) == _outcome(baseline)


# ------------------------------------------------------------------ observations


def _valid_fields() -> dict[str, object]:
    return {
        "source": "jsonld",
        "ownership": Ownership.UNKNOWN,
        "node_ref": "jsonld:0",
        "node_key": None,
        "price": Money(Decimal("10"), EUR),
        "unreadable_reason": "",
    }


WRONG_FIELDS: dict[str, list[object]] = {
    "source": [None, 1, b"jsonld", "", "x" * 3000],
    "ownership": ["requested", None, 1, AnchorState.FOUND],
    "node_ref": [None, 1, "", "r" * 3000],
    "node_key": [1, "", b"k", ["k"]],
    "price": [Decimal("10"), 10, "10", 10.0, (Decimal("10"), "EUR")],
    "unreadable_reason": [None, 1, "because"],
}


@settings(max_examples=300, deadline=None)
@given(
    st.sampled_from(sorted(WRONG_FIELDS)).flatmap(
        lambda f: st.tuples(st.just(f), st.sampled_from(WRONG_FIELDS[f]))
    )
)
def test_observation_rejects_wrong_fields(case: tuple[str, object]) -> None:
    field, value = case
    fields = _valid_fields()
    Observation(**fields)  # type: ignore[arg-type]  # control
    fields[field] = value
    with pytest.raises((TypeError, ValueError)):
        Observation(**fields)  # type: ignore[arg-type]


def test_observation_price_and_reason_are_exclusive() -> None:
    with pytest.raises(ValueError, match="must name an unreadable reason"):
        Observation("jsonld", Ownership.UNKNOWN, "r", None, None)
    with pytest.raises(ValueError, match="cannot carry an unreadable reason"):
        Observation("jsonld", Ownership.UNKNOWN, "r", None, Money(Decimal(1), EUR), "malformed")
    assert Observation("jsonld", Ownership.UNKNOWN, "r", None, None, "malformed").price is None


MONEY_BREAKS: list[tuple[object, object]] = [
    (10.0, EUR),
    (10, EUR),
    (True, EUR),
    ("10", EUR),
    (Decimal("NaN"), EUR),
    (Decimal("Infinity"), EUR),
    (Decimal(0), EUR),
    (Decimal(-1), EUR),
    (Decimal(10) ** 9 + 1, EUR),
    (Decimal("1.234"), EUR),
    (Decimal("1.5"), "JPY"),
    (Decimal("1.2345"), None),
    (Decimal(10), "eur"),
    (Decimal(10), "XTS"),
    (Decimal(10), "EURO"),
    (Decimal(10), 978),
]


@pytest.mark.parametrize(("amount", "currency"), MONEY_BREAKS, ids=repr)
def test_money_rejects_invalid_values(amount: object, currency: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        Money(amount, currency)  # type: ignore[arg-type]


def test_money_accepts_valid_values() -> None:
    assert Money(Decimal("1.234"), "BHD").amount == Decimal("1.234")
    assert Money(Decimal("1.230"), EUR).amount == Decimal("1.23")
    assert Money(Decimal("1.234"), None).currency is None
    assert Money(Decimal(10) ** 9, "JPY").amount == Decimal(10) ** 9


# ------------------------------------------------------------------ resolver states


def test_every_state_reason_price_combination() -> None:
    price = Money(Decimal("10"), EUR)
    all_reasons = sorted(set().union(*REASONS_BY_STATE.values()))
    constructed = 0
    for state in AnchorState:
        for reason in [*all_reasons, "", "unheard_of"]:
            for with_price in (False, True):
                valid = reason in REASONS_BY_STATE[state] and with_price == (
                    state is AnchorState.FOUND
                )
                kwargs = {"price": price if with_price else None, "reason": reason}
                if valid:
                    AnchorResult(state, **kwargs)  # type: ignore[arg-type]
                    constructed += 1
                else:
                    with pytest.raises(ValueError, match="reason|price"):
                        AnchorResult(state, **kwargs)  # type: ignore[arg-type]
    assert constructed == sum(len(r) for r in REASONS_BY_STATE.values())


def test_anchor_result_rejects_wrong_types() -> None:
    with pytest.raises(TypeError):
        AnchorResult("found", Money(Decimal(1), EUR), "owned")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AnchorResult(AnchorState.NO_CANDIDATE, None, "no_observation", deciding=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AnchorResult(AnchorState.NO_CANDIDATE, None, "no_observation", deciding=(1,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="FOUND requires a price"):
        AnchorResult(AnchorState.FOUND, Decimal(1), "owned")  # type: ignore[arg-type]


def test_resolver_rejects_wrong_input_types() -> None:
    good = Observation("jsonld", Ownership.UNKNOWN, "r", None, Money(Decimal(1), EUR))
    assert resolve_anchor((good,)).state is AnchorState.FOUND
    for bad in ([good], iter([good]), (good, None), (good, {"price": 1}), "obs"):
        with pytest.raises(TypeError):
            resolve_anchor(bad)  # type: ignore[arg-type]


def test_too_many_nodes_counts_distinct_observations() -> None:
    def node(i: int) -> Observation:
        return Observation("jsonld", Ownership.UNKNOWN, f"r{i}", None, Money(Decimal(10), EUR))

    over = tuple(node(i) for i in range(MAX_OBSERVATIONS + 1))
    at_limit = over[:MAX_OBSERVATIONS]
    assert resolve_anchor(over).reason == "too_many_nodes"
    assert resolve_anchor(over).state is AnchorState.AMBIGUOUS
    assert resolve_anchor(at_limit).reason == "multiple_unknown_nodes"
    copies = (node(0),) * (MAX_OBSERVATIONS + 5)
    assert resolve_anchor(copies).state is AnchorState.FOUND


_f4_a = "https://shop.example.com/p/A"
_f4_b = "https://shop.example.com/p/B"
_f4_requested = RequestedIdentity.from_url(_f4_a)


def _f4_product(**fields: object) -> dict[str, object]:
    return {"@type": "Product", "offers": {"price": "9.99", "priceCurrency": "EUR"}, **fields}


def _f4_anchor(doc: object):
    return anchor_structured_page([json.dumps(doc)], _f4_requested)


@settings(max_examples=50, database=None)
@given(st.integers(min_value=2049, max_value=3000), st.sampled_from(["sku", "url", "pointer"]))
def test_long_untrusted_labels_are_terminal(size, field):
    doc = (
        {"x" * size: _f4_product(url=_f4_a)}
        if field == "pointer"
        else _f4_product(**{field: "x" * size})
    )
    result = anchor_structured_page(
        [json.dumps(doc), json.dumps(_f4_product(url=_f4_a))], _f4_requested
    )
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "malformed_structure",
        None,
    )


@settings(max_examples=40, database=None)
@given(st.integers(min_value=0xD800, max_value=0xDFFF))
def test_surrogate_identity_never_escapes(code):
    result = anchor_structured_page(
        [
            json.dumps(_f4_product(url=_f4_a + "?q=" + chr(code))),
            json.dumps(_f4_product(url=_f4_a)),
        ],
        _f4_requested,
    )
    assert result.state is not AnchorState.FOUND
    assert result.price is None
    assert normalize_url(_f4_a + "?q=" + chr(code)) is None


@pytest.mark.parametrize("exponent", ["99999999999999999999", "-99999999999999999999"])
def test_unrepresentable_json_decimal_is_terminal(exponent):
    bad = '{"@type":"Product","offers":{"price":1e' + exponent + "}}"
    result = anchor_structured_page([bad, json.dumps(_f4_product(url=_f4_a))], _f4_requested)
    assert (result.state, result.reason, result.price) == (
        AnchorState.AMBIGUOUS,
        "malformed_structure",
        None,
    )


@settings(max_examples=40, database=None)
@given(st.integers(min_value=65, max_value=10000))
def test_decimal_expansion_is_bounded(exponent):
    assert decode_price_value(Decimal((0, (1,), -exponent))) is None
    assert decode_price_value(Decimal("19.90")) is not None
