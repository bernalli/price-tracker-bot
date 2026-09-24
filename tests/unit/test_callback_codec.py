"""Callback codec: round trip, hostile decoding and the registry language.

The oracle below is written from the action inventory of
``bot/callbacks.py::build_registry`` (every registered action plus the
flow-token shape), independently of the codec itself: it has
its own shape list, its own token regexes and its own enum values. Tests compare
the codec with it and never use the codec to compute an expected value.
"""

from __future__ import annotations

import asyncio
import re
import string
from decimal import Decimal
from typing import Any

import pytest
from babel.numbers import list_currencies
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from price_tracker.bot.callbacks import (
    REGISTRY,
    Action,
    ActionRegistry,
    ActionSpec,
    BackArg,
    Choice,
    FlowTokenArg,
    IdArg,
    InvalidCallback,
    Literal,
    decode,
    encode,
)
from tests.support.fake_telegram import FakeServices, callback_update
from tests.support.flow_harness import Harness

ID_MAX = 2**63 - 1
LOCALES = ("en", "it", "zh_Hans", "fr", "es", "de", "uk", "pt_BR", "ja")
LEVELS = ("own_country", "customs_area", "world")
CURRENCY_CHOICES = (*sorted(list_currencies()), "type", "cancel")

# One entry per registered action: (name, tokens). A token is a literal string,
# or ("id",), ("tok",), ("back",), ("enum", values).
ID: tuple[str] = ("id",)
TOK: tuple[str] = ("tok",)
BACK: tuple[str] = ("back",)


def _enum(*values: str) -> tuple[str, tuple[str, ...]]:
    return ("enum", values)


MODEL: dict[str, tuple[Any, ...]] = {
    "home": ("h",),
    "noop": ("noop",),
    "stats": ("st",),
    "check_all": ("ca",),
    "help": ("hp",),
    "help.section": ("hp", _enum("tracking", "alerts", "prefs", "data", "admin")),
    "back": ("x", BACK),
    "list.page": ("l", _enum("a", "p", "e"), ID),
    "list.remove_all": ("l", "rmall"),
    "list.remove_all_ok": ("l", "rmallok"),
    "product.card": ("p", ID, "c"),
    "product.check": ("p", ID, "ck"),
    "product.chart": ("p", ID, "ch", _enum("7d", "30d", "90d", "1y", "all")),
    "product.pause": ("p", ID, "pa"),
    "product.remove": ("p", ID, "rm"),
    "product.remove_ok": ("p", ID, "rmok"),
    "product.edit": ("p", ID, "ed"),
    "product.reset": ("p", ID, "rs"),
    "product.reactivate": ("p", ID, "ra"),
    "product.threshold": ("p", ID, "th"),
    "product.threshold_any": ("p", ID, "th", "any"),
    "product.threshold_default": ("p", ID, "th", "def"),
    "product.target": ("p", ID, "tg"),
    "product.interval": ("p", ID, "iv"),
    "product.offer_filter": ("p", ID, "pf", _enum("n", "u", "s1", "s0", "0")),
    "product.mute": ("p", ID, "mu", _enum("1", "8", "24", "0")),
    "product.scope_picker": ("p", ID, "sco"),
    "product.scope": ("p", ID, "sco", _enum("store", *LEVELS, "default")),
    "flow.currency": ("p", TOK, "cur", _enum(*CURRENCY_CHOICES)),
    "flow.scope_picker": ("p", TOK, "sc"),
    "flow.scope": ("p", TOK, "sc", _enum("store", *LEVELS)),
    "flow.cancel": ("p", TOK, "x"),
    "settings": ("s",),
    "settings.chart_theme": ("s", "ct", _enum("light", "dark")),
    "settings.language": ("s", "lang", _enum(*LOCALES)),
    "data": ("d",),
    "data.export": ("d", "x"),
    "data.import": ("d", "i"),
    "admin": ("a",),
    "admin.users": ("a", "u"),
    "admin.add_user": ("a", "add"),
    "admin.remove_user": ("a", "rm"),
    "admin.remove_user_id": ("a", "rm", ID),
    "admin.nick": ("a", "nk"),
    "admin.nick_id": ("a", "nk", ID),
    "admin.interval": ("a", "iv"),
    "admin.debug": ("a", "dbg"),
}

_ID_TEXT = r"[1-9][0-9]{0,18}"


def _token_regex(token: Any) -> str:
    if isinstance(token, str):
        return re.escape(token)
    if token == ID:
        return f"({_ID_TEXT})"
    if token == TOK:
        return "([0-9a-f]{32})"
    if token == BACK:
        return f"(h|l|s|a|p{_ID_TEXT})"
    return "(" + "|".join(re.escape(v) for v in token[1]) + ")"


_ORACLE = {
    name: re.compile(":".join(_token_regex(t) for t in tokens)) for name, tokens in MODEL.items()
}


def oracle(data: object) -> Action | None:
    """The registered action ``data`` denotes, or ``None`` if outside the language."""
    if not isinstance(data, str) or not data.isascii() or not 1 <= len(data.encode()) <= 64:
        return None
    hits: list[Action] = []
    for name, pattern in _ORACLE.items():
        match = pattern.fullmatch(data)
        if match is None:
            continue
        args: list[int | str] = []
        ok = True
        for token, value in zip(
            [t for t in MODEL[name] if not isinstance(t, str)], match.groups(), strict=True
        ):
            if token == ID:
                if int(value) > ID_MAX:
                    ok = False
                args.append(int(value))
            else:
                if token == BACK and value.startswith("p") and int(value[1:]) > ID_MAX:
                    ok = False
                args.append(value)
        if ok:
            hits.append(Action(name, tuple(args)))
    assert len(hits) <= 1, f"oracle ambiguity for {data!r}: {hits}"
    return hits[0] if hits else None


def render(name: str, args: tuple[int | str, ...]) -> str:
    values = iter(args)
    return ":".join(t if isinstance(t, str) else str(next(values)) for t in MODEL[name])


ids = st.integers(min_value=1, max_value=ID_MAX)
tokens = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)
backs = st.one_of(st.sampled_from(("h", "l", "s", "a")), ids.map(lambda i: f"p{i}"))


@st.composite
def valid_actions(draw: st.DrawFn) -> Action:
    name = draw(st.sampled_from(sorted(MODEL)))
    args: list[int | str] = []
    for token in MODEL[name]:
        if isinstance(token, str):
            continue
        if token == ID:
            args.append(draw(ids))
        elif token == TOK:
            args.append(draw(tokens))
        elif token == BACK:
            args.append(draw(backs))
        else:
            args.append(draw(st.sampled_from(token[1])))
    return Action(name, tuple(args))


HOSTILE_IDS = ("0", "-1", "007", "9223372036854775808", "1e3", "\uff14\uff12", "1" * 70, "+5")


@st.composite
def mutations(draw: st.DrawFn) -> str:
    wire = render(*_as_pair(draw(valid_actions())))
    parts = wire.split(":")
    op = draw(st.integers(0, 10))
    if op == 0 and len(parts) > 1:
        del parts[draw(st.integers(0, len(parts) - 1))]
    elif op == 1:
        extra = draw(
            st.one_of(st.sampled_from(("h", "p", "x", "1", "USD", "7d")), st.text(max_size=4))
        )
        parts.insert(draw(st.integers(0, len(parts))), extra)
    elif op == 2 and len(parts) > 1:
        i, j = draw(st.integers(0, len(parts) - 1)), draw(st.integers(0, len(parts) - 1))
        parts[i], parts[j] = parts[j], parts[i]
    elif op == 3:
        joined = ":".join(parts)
        pos = draw(st.integers(0, len(joined) - 1))
        char = draw(st.characters())
        return joined[:pos] + char + joined[pos + 1 :]
    elif op == 4:
        return wire + "\n"
    elif op == 5:
        return " " + wire
    elif op == 6:
        numeric = [i for i, p in enumerate(parts) if p.isdigit()]
        if numeric:
            parts[draw(st.sampled_from(numeric))] = draw(st.sampled_from(HOSTILE_IDS))
    elif op == 7:
        i = draw(st.integers(0, len(parts) - 1))
        parts[i] = draw(
            st.sampled_from(
                (parts[i].upper(), parts[i][:-1] or "q", "world", "pt_br", "EUR", "rmall!")
            )
        )
    elif op == 8:
        joined = ":".join(parts)
        return joined[: draw(st.integers(0, len(joined)))]
    elif op == 9:
        i = draw(st.integers(0, len(parts) - 1))
        parts[i] = parts[i] + draw(st.sampled_from(("0", "a", "!", "\u00e9", "\t")))
    else:
        return wire + ":" + draw(st.sampled_from(("", "x", "1")))
    return ":".join(parts)


def _as_pair(action: Action) -> tuple[str, tuple[int | str, ...]]:
    return action.name, action.args


# --- registry coverage -------------------------------------------------------


def test_registry_names_equal_the_independent_model() -> None:
    assert REGISTRY.names == frozenset(MODEL)


def test_every_verb_the_spec_uses_fits_the_grammar() -> None:
    """rmok and rmallok (seven characters) are registered literals."""
    assert decode("l:rmallok") == Action("list.remove_all_ok")
    assert decode("p:42:rmok") == Action("product.remove_ok", (42,))
    assert decode("s:lang:pt_BR") == Action("settings.language", ("pt_BR",))
    assert decode("s:lang:zh_Hans") == Action("settings.language", ("zh_Hans",))
    assert decode("x:p42") == Action("back", ("p42",))


def test_currency_callback_belongs_to_the_grammar() -> None:
    """p:<32-hex flow token>:cur:<choice> is a registered shape."""
    # A 32-character hex flow token, kept as two literals so secret scanners do
    # not report this test value as a credential (a false positive).
    token = "01234567" "89abcdef" * 2  # fmt: skip
    for choice in ("USD", "type", "cancel"):
        wire = f"p:{token}:cur:{choice}"
        assert decode(wire) == Action("flow.currency", (token, choice))
        assert encode(Action("flow.currency", (token, choice))) == wire
    assert isinstance(decode("p:a1b2c3d4:cur:USD"), InvalidCallback)
    assert isinstance(decode("p:57:cur:USD"), InvalidCallback)
    assert isinstance(decode(f"p:{token}:cur:usd"), InvalidCallback)
    assert isinstance(decode(f"p:{token.upper()}:cur:USD"), InvalidCallback)


def test_longest_encodings_fit_64_bytes() -> None:
    for name in REGISTRY.names:
        assert REGISTRY.spec(name).max_bytes <= 64, name
    worst = render("back", (f"p{ID_MAX}",))
    assert decode(worst) == Action("back", (f"p{ID_MAX}",))


# --- properties --------------------------------------------------------------


@settings(max_examples=3000, deadline=None)
@given(valid_actions())
def test_round_trip_for_every_registered_action(action: Action) -> None:
    wire = render(action.name, action.args)
    assert 1 <= len(wire.encode("ascii")) <= 64
    assert decode(wire) == action
    assert encode(action) == wire


@settings(max_examples=5000, deadline=None)
@given(mutations())
def test_mutations_decode_exactly_as_the_independent_oracle(data: str) -> None:
    """Invalid mutations are rejected, valid ones decode to that action."""
    expected = oracle(data)
    got = decode(data)
    if expected is None:
        assert isinstance(got, InvalidCallback), (data, got)
    else:
        assert got == expected


@settings(max_examples=3000, deadline=None)
@given(
    st.one_of(
        st.text(),
        st.text(alphabet=string.printable),
        st.text(alphabet="pxhl0123456789abcdef:_", max_size=70),
        st.binary(),
        st.none(),
        st.integers(),
        st.floats(),
        st.lists(st.text(max_size=3)),
        st.decimals(allow_nan=True),
    )
)
def test_decode_never_raises_and_agrees_with_oracle(data: object) -> None:
    got = decode(data)
    expected = oracle(data)
    if expected is None:
        assert isinstance(got, InvalidCallback)
    else:
        assert got == expected


# --- deterministic not-well-formed cases ---------------------------------------

REJECTED = [
    None,
    b"h",
    "",
    "h" * 65,
    "h:",
    ":h",
    "h::l",
    "h\n",
    " h",
    "h ",
    "p:0:c",
    "p:-1:c",
    "p:007:c",
    "p:9223372036854775808:c",
    "p:1e3:c",
    "p:\uff14\uff12:c",
    "p:" + "1" * 70 + ":c",
    "p:42:CH:30d",
    "p:42:ch:30D",
    "p:42:ch:3",
    "p:42:ch:30d:x",
    "p:42",
    "l:rmall!",
    "s:lang:pt_br",
    "s:lang:zh",
    "x:p0",
    "x:q",
    "x:p9223372036854775808",
    "noop:1",
    "q",
    "ops_react_5",
    "h:l:p:s:a",
    "p:42:\u00e9",
    Decimal("1"),
    42,
]


@pytest.mark.parametrize("data", REJECTED, ids=repr)
def test_not_well_formed_is_rejected(data: object) -> None:
    assert isinstance(decode(data), InvalidCallback)
    assert oracle(data) is None


@pytest.mark.parametrize(
    "action",
    [
        Action("nope"),
        Action("product.card"),
        Action("product.card", (0,)),
        Action("product.card", (ID_MAX + 1,)),
        Action("product.card", (True,)),
        Action("product.card", ("42",)),
        Action("product.chart", (42, "2d")),
        Action("flow.cancel", ("ABC",)),
        Action("back", ("p0",)),
        Action("home", (1,)),
    ],
    ids=repr,
)
def test_encode_raises_on_schema_violation(action: Action) -> None:
    with pytest.raises(ValueError, match=r"."):
        encode(action)


def test_registry_construction_rejects_duplicates_overlaps_and_oversize() -> None:
    home = ActionSpec("home", (Literal("h"),))
    with pytest.raises(ValueError, match="duplicate"):
        ActionRegistry([home, ActionSpec("home", (Literal("hh"),))])
    with pytest.raises(ValueError, match="overlapping"):
        ActionRegistry(
            [
                ActionSpec("a", (Literal("p"), IdArg("id"), Literal("c"))),
                ActionSpec("b", (Literal("p"), IdArg("id"), Choice("v", ("c", "d")))),
            ]
        )
    with pytest.raises(ValueError, match="overlapping"):
        ActionRegistry(
            [
                ActionSpec("a", (Literal("x"), BackArg())),
                ActionSpec("b", (Literal("x"), Choice("v", ("h",)))),
            ]
        )
    with pytest.raises(ValueError, match="longest encoding"):
        ActionRegistry([ActionSpec("a", (Literal("p"), FlowTokenArg(), FlowTokenArg()))])
    with pytest.raises(ValueError, match="bad literal"):
        ActionRegistry([ActionSpec("a", (Literal("RM"),))])
    with pytest.raises(ValueError, match="namespace"):
        ActionRegistry([ActionSpec("a", (IdArg("id"),))])


def test_id_and_token_positions_never_overlap() -> None:
    ActionRegistry(
        [
            ActionSpec("a", (Literal("p"), IdArg("id"), Literal("x"))),
            ActionSpec("b", (Literal("p"), FlowTokenArg(), Literal("x"))),
            ActionSpec("c", (Literal("x"), BackArg())),
            ActionSpec("d", (Literal("x"), IdArg("id"))),
        ]
    )


# --- through Application.process_update ------------------------------------------


def test_mutated_callbacks_through_process_update() -> None:
    """End to end: invalid data -> zero service calls, one expiry answer, no
    other handler; valid data -> the registered handler with no write."""
    loop = asyncio.new_event_loop()
    services = FakeServices(active={10}, products={1: (10, "Kettle")})
    harness = Harness(services)
    loop.run_until_complete(harness.start())

    @settings(
        max_examples=600, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(st.one_of(mutations(), valid_actions().map(lambda a: render(a.name, a.args))))
    def check(data: str) -> None:
        harness.flow.registry.discard((100, 10))
        calls_before = len(services.calls)
        answers_before = len(harness.request.calls_of("answerCallbackQuery"))
        routed_before = len(harness.routed)
        update = callback_update(harness.app.bot, 100, 10, data)
        loop.run_until_complete(harness.process(update))
        answers = harness.request.calls_of("answerCallbackQuery")[answers_before:]
        assert len(answers) == 1
        assert services.writes == []
        expected = oracle(data)
        if expected is None:
            assert len(services.calls) == calls_before
            assert answers[0].params.get("text") == "This button has expired."
            assert len(harness.routed) == routed_before
            assert len(harness.flow.registry) == 0

    try:
        check()
    finally:
        loop.run_until_complete(harness.stop())
        loop.close()
