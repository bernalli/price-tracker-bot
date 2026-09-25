"""Entry buttons emitted before the action registry map onto registry actions.

The threshold, target and interval buttons of the product list, the picker and
the edit screen still carry ``<prefix>_<id>`` data. ``decode_legacy_entry``
turns exactly those five prefixes, followed by a canonical decimal id, into the
registry action that opens the matching guided prompt, and rejects everything
else without raising.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from price_tracker.bot.callbacks import (
    ID_MAX,
    REGISTRY,
    Action,
    Choice,
    FlowTokenArg,
    IdArg,
    InvalidCallback,
    Literal,
    decode_legacy_entry,
)

# Written from the list of legacy emitters, not imported from the module.
PREFIX_ACTIONS: dict[str, str] = {
    "setsoglia": "product.threshold",
    "track_threshold": "product.threshold",
    "settarget": "product.target",
    "track_target": "product.target",
    "setrefresh": "product.interval",
}
VERB_OF_ACTION = {"product.threshold": "th", "product.target": "tg", "product.interval": "iv"}


def _oracle(data: object) -> Action | None:
    """Independent reading of the grammar: string methods only, no regular expression."""
    if not isinstance(data, str) or not data or not data.isascii():
        return None
    if len(data.encode("ascii")) > 64:
        return None
    prefix, separator, digits = data.rpartition("_")
    if not separator or prefix not in PREFIX_ACTIONS:
        return None
    if not digits or not digits.isdigit() or digits[0] == "0":
        return None
    if len(digits) > 19 or int(digits) > ID_MAX:
        return None
    return Action(PREFIX_ACTIONS[prefix], (int(digits),))


@pytest.mark.parametrize("prefix", sorted(PREFIX_ACTIONS))
@pytest.mark.parametrize("product_id", [1, 42, ID_MAX])
def test_each_prefix_maps_to_its_registry_action(prefix: str, product_id: int) -> None:
    assert decode_legacy_entry(f"{prefix}_{product_id}") == Action(
        PREFIX_ACTIONS[prefix], (product_id,)
    )


NOT_WELL_FORMED: list[object] = [
    None,
    42,
    b"setsoglia_1",
    "",
    "setsoglia_",
    "setsoglia_0",
    "setsoglia_01",
    "setsoglia_-1",
    "setsoglia_+1",
    "setsoglia_ 1",
    "setsoglia_1 ",
    "setsoglia_1\n",
    "setsoglia_#1",
    "setsoglia_١",
    "setsoglia_１",
    "setsoglia_1_2",
    "xsetsoglia_1",
    "SETSOGLIA_1",
    "setsoglia1",
    f"setsoglia_{ID_MAX + 1}",
    "setsoglia_" + "1" * 20,
    "track_any_1",
    "track_default_1",
    "setsoglia_1" + "0" * 60,
    "p:1:th",
]


@pytest.mark.parametrize("data", NOT_WELL_FORMED, ids=repr)
def test_not_well_formed_data_is_rejected(data: object) -> None:
    assert decode_legacy_entry(data) is None


_MUTATED_PREFIXES = st.sampled_from(
    [
        *PREFIX_ACTIONS,
        "setSoglia",
        "SETSOGLIA",
        "track",
        "track_any",
        "track_default",
        "setsoglia_",
        "xsettarget",
        "",
        "setrefresh ",
    ]
)
_MUTATED_IDS = st.one_of(
    st.integers(min_value=-5, max_value=ID_MAX + 5).map(str),
    st.text(alphabet="0123456789+-# \n١１_", max_size=22),
    st.integers(min_value=1, max_value=ID_MAX).map(lambda n: f"0{n}"),
    st.integers(min_value=1, max_value=ID_MAX).map(lambda n: f"{n}\n"),
)
_CONSTRUCTED = st.builds(lambda prefix, ident: f"{prefix}_{ident}", _MUTATED_PREFIXES, _MUTATED_IDS)
_ANYTHING = st.one_of(
    st.text(), st.binary(), st.integers(), st.none(), st.floats(allow_nan=True), _CONSTRUCTED
)


@given(_ANYTHING)
def test_decoder_never_raises_and_agrees_with_an_independent_oracle(data: Any) -> None:
    assert decode_legacy_entry(data) == _oracle(data)


@given(st.sampled_from(sorted(PREFIX_ACTIONS)), st.integers(min_value=1, max_value=ID_MAX))
def test_accepted_entry_round_trips_through_the_registry(prefix: str, product_id: int) -> None:
    action = decode_legacy_entry(f"{prefix}_{product_id}")

    assert action is not None
    wire = REGISTRY.encode(action)
    assert wire == f"p:{product_id}:{VERB_OF_ACTION[action.name]}"
    assert REGISTRY.decode(wire) == action


@given(st.sampled_from(sorted(PREFIX_ACTIONS)), st.integers(min_value=1, max_value=ID_MAX))
def test_accepted_legacy_data_is_not_registry_data(prefix: str, product_id: int) -> None:
    data = f"{prefix}_{product_id}"

    assert decode_legacy_entry(data) is not None
    assert isinstance(REGISTRY.decode(data), InvalidCallback)


def _sample_args(name: str) -> tuple[int | str, ...]:
    args: list[int | str] = []
    for kind in REGISTRY.spec(name).kinds:
        if isinstance(kind, Literal):
            continue
        if isinstance(kind, Choice):
            args.append(kind.values[0])
        elif isinstance(kind, IdArg):
            args.append(1)
        elif isinstance(kind, FlowTokenArg):
            args.append("0" * 32)
        else:
            args.append("h")
    return tuple(args)


@pytest.mark.parametrize("name", sorted(REGISTRY.names))
def test_registry_data_is_not_legacy_entry_data(name: str) -> None:
    wire = REGISTRY.encode(Action(name, _sample_args(name)))

    assert decode_legacy_entry(wire) is None
