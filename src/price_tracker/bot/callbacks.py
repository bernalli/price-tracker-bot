"""Callback data: one codec over a closed action registry.

The registry *is* the grammar. Each entry fixes the namespace, the token count,
the literals and the kind of every argument position; there is no generic
"verb" class, so a literal such as ``rmallok`` is legal because an entry declares
it, and nothing else of that shape is. Construction rejects duplicate names,
overlapping shapes and any shape whose longest possible encoding exceeds the
Telegram limit of :data:`MAX_CALLBACK_BYTES` bytes.

Wire form: 1..64 ASCII bytes; 1..4 non-empty colon-separated tokens; no
whitespace or control characters.

Flow-scoped callbacks (currency and scope choices, the in-flow cancel) carry the
full flow token (``uuid4().hex``, 32 lowercase hex characters) instead of a
product id: ``p:<flow_token>:cur:<choice>``, ``p:<flow_token>:sc[:<choice>]`` and
``p:<flow_token>:x``. The decimal-id rule applies only to positions declared as
ids, and an id (1..19 digits) can never be confused with a token (32 characters).

:func:`decode` accepts untrusted input and never raises: anything that is not a
complete registered action yields :class:`InvalidCallback`. :func:`encode` raises
``ValueError`` on any violation, which is a programming error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING, Final

from babel.numbers import list_currencies

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_CALLBACK_BYTES: Final = 64
MAX_TOKENS: Final = 4
ID_MAX: Final = 9_223_372_036_854_775_807
ID_MAX_DIGITS: Final = len(str(ID_MAX))
FLOW_TOKEN_CHARS: Final = 32

SUPPORTED_LOCALES: Final = ("en", "it", "zh_Hans", "fr", "es", "de", "uk", "pt_BR", "ja")
SCOPE_LEVELS: Final = ("own_country", "customs_area", "world")
FLOW_SCOPE_CHOICES: Final = ("store", *SCOPE_LEVELS)
CARD_SCOPE_CHOICES: Final = ("store", *SCOPE_LEVELS, "default")
PERIODS: Final = ("7d", "30d", "90d", "1y", "all")
THEMES: Final = ("light", "dark")
MUTE_PRESETS: Final = ("1", "8", "24", "0")
LIST_FILTERS: Final = ("a", "p", "e")
OFFER_FILTERS: Final = ("n", "u", "s1", "s0", "0")
HELP_SECTIONS: Final = ("tracking", "alerts", "prefs", "data", "admin")
CURRENCY_CHOICE_LITERALS: Final = ("type", "cancel")

_ID_RE: Final = re.compile(r"[1-9][0-9]{0,18}")
_TOKEN_RE: Final = re.compile(r"[0-9a-f]{32}")
_WIRE_TOKEN_RE: Final = re.compile(r"[\x21-\x39\x3b-\x7e]+")
_LITERAL_RE: Final = re.compile(r"[a-z][a-z0-9]{0,7}")


class InvalidReason(StrEnum):
    """Why :func:`decode` rejected a callback."""

    NOT_A_STRING = "not_a_string"
    EMPTY = "empty"
    NOT_ASCII = "not_ascii"
    TOO_LONG = "too_long"
    BAD_TOKENS = "bad_tokens"
    UNKNOWN_ACTION = "unknown_action"


@dataclass(frozen=True, slots=True)
class InvalidCallback:
    """Typed rejection returned by :func:`decode`; never raised."""

    reason: InvalidReason


@dataclass(frozen=True, slots=True)
class Action:
    """A registered action: the entry name and its argument values in order.

    Argument values are ``int`` for id positions and ``str`` for every other
    variable position (enums, flow tokens, back targets).
    """

    name: str
    args: tuple[int | str, ...] = ()


# --- argument kinds --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Literal:
    """A fixed token."""

    text: str


@dataclass(frozen=True, slots=True)
class Choice:
    """A finite enum of tokens."""

    label: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IdArg:
    """A canonical decimal integer in 1..ID_MAX (no sign, no leading zero)."""

    label: str


@dataclass(frozen=True, slots=True)
class FlowTokenArg:
    """Exactly 32 lowercase hexadecimal characters (``uuid4().hex``)."""


@dataclass(frozen=True, slots=True)
class BackArg:
    """A back target: ``h``, ``l``, ``s``, ``a`` or ``p<id>``."""


Kind = Literal | Choice | IdArg | FlowTokenArg | BackArg

BACK_FIXED: Final = ("h", "l", "s", "a")


def _parse_id(token: str) -> int | None:
    if _ID_RE.fullmatch(token) is None:
        return None
    value = int(token)
    return value if value <= ID_MAX else None


def _accepts(kind: Kind, token: str) -> int | str | None:
    """Return the argument value for ``token``, or ``None`` if not accepted."""
    if isinstance(kind, Literal):
        return token if token == kind.text else None
    if isinstance(kind, Choice):
        return token if token in kind.values else None
    if isinstance(kind, IdArg):
        return _parse_id(token)
    if isinstance(kind, FlowTokenArg):
        return token if _TOKEN_RE.fullmatch(token) is not None else None
    if token in BACK_FIXED:
        return token
    if token.startswith("p") and _parse_id(token[1:]) is not None:
        return token
    return None


def _render(kind: Kind, value: int | str) -> str:
    """Render one argument; raises ``ValueError`` when the value is not accepted."""
    if isinstance(kind, Literal):
        raise ValueError("literals take no argument")  # pragma: no cover - internal misuse
    if isinstance(kind, IdArg):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{kind.label} must be an int, got {value!r}")
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError(f"argument must be a str, got {value!r}")
    if _accepts(kind, text) is None:
        raise ValueError(f"value {value!r} is not accepted by {kind!r}")
    return text


def _max_len(kind: Kind) -> int:
    if isinstance(kind, Literal):
        return len(kind.text)
    if isinstance(kind, Choice):
        return max(len(value) for value in kind.values)
    if isinstance(kind, IdArg):
        return ID_MAX_DIGITS
    if isinstance(kind, FlowTokenArg):
        return FLOW_TOKEN_CHARS
    return 1 + ID_MAX_DIGITS


def _finite_values(kind: Kind) -> tuple[str, ...] | None:
    if isinstance(kind, Literal):
        return (kind.text,)
    if isinstance(kind, Choice):
        return kind.values
    return None


def _infinite_overlap(left: Kind, right: Kind) -> bool:
    """Whether two infinite kinds share a token.

    Ids are 1..19 decimal digits, flow tokens exactly 32 hex characters, and the
    infinite part of a back target starts with ``p`` (never a digit, never hex),
    so only kinds of the same class overlap.
    """
    return type(left) is type(right)


def kinds_overlap(left: Kind, right: Kind) -> bool:
    """Whether some token is accepted by both kinds."""
    left_values = _finite_values(left)
    if left_values is not None:
        return any(_accepts(right, value) is not None for value in left_values)
    right_values = _finite_values(right)
    if right_values is not None:
        return any(_accepts(left, value) is not None for value in right_values)
    if isinstance(left, BackArg) or isinstance(right, BackArg):
        other = right if isinstance(left, BackArg) else left
        return isinstance(other, BackArg)
    return _infinite_overlap(left, right)


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """One registry entry: a name and the kind of every token position."""

    name: str
    kinds: tuple[Kind, ...]
    flow: bool = False

    @property
    def max_bytes(self) -> int:
        """Longest possible encoding, separators included."""
        return sum(_max_len(kind) for kind in self.kinds) + len(self.kinds) - 1

    @property
    def arity(self) -> int:
        """Number of variable (non-literal) positions."""
        return sum(1 for kind in self.kinds if not isinstance(kind, Literal))


def specs_overlap(left: ActionSpec, right: ActionSpec) -> bool:
    """Whether some wire string would match both entries."""
    if len(left.kinds) != len(right.kinds):
        return False
    return all(kinds_overlap(a, b) for a, b in zip(left.kinds, right.kinds, strict=True))


class ActionRegistry:
    """A closed, validated set of :class:`ActionSpec`."""

    def __init__(self, specs: Iterable[ActionSpec]) -> None:
        entries = tuple(specs)
        by_name: dict[str, ActionSpec] = {}
        for spec in entries:
            self._validate_spec(spec)
            if spec.name in by_name:
                raise ValueError(f"duplicate action name {spec.name!r}")
            by_name[spec.name] = spec
        for index, left in enumerate(entries):
            for right in entries[index + 1 :]:
                if specs_overlap(left, right):
                    raise ValueError(f"overlapping action shapes {left.name!r} and {right.name!r}")
        self._by_name = by_name
        self._by_count: dict[int, tuple[ActionSpec, ...]] = {
            count: tuple(spec for spec in entries if len(spec.kinds) == count)
            for count in range(1, MAX_TOKENS + 1)
        }

    @staticmethod
    def _validate_spec(spec: ActionSpec) -> None:
        if not 1 <= len(spec.kinds) <= MAX_TOKENS:
            raise ValueError(f"{spec.name!r}: 1..{MAX_TOKENS} tokens required")
        if not isinstance(spec.kinds[0], Literal):
            raise ValueError(f"{spec.name!r}: the namespace must be a literal")
        for kind in spec.kinds:
            if isinstance(kind, Literal) and _LITERAL_RE.fullmatch(kind.text) is None:
                raise ValueError(f"{spec.name!r}: bad literal {kind.text!r}")
            values = _finite_values(kind)
            if values is not None:
                if not values or len(set(values)) != len(values):
                    raise ValueError(f"{spec.name!r}: empty or duplicate enum")
                for value in values:
                    if _WIRE_TOKEN_RE.fullmatch(value) is None:
                        raise ValueError(f"{spec.name!r}: bad enum value {value!r}")
        if spec.max_bytes > MAX_CALLBACK_BYTES:
            raise ValueError(
                f"{spec.name!r}: longest encoding is {spec.max_bytes} bytes "
                f"(limit {MAX_CALLBACK_BYTES})"
            )

    @property
    def names(self) -> frozenset[str]:
        """Every registered action name."""
        return frozenset(self._by_name)

    def spec(self, name: str) -> ActionSpec:
        """The entry called ``name``; ``KeyError`` if absent."""
        return self._by_name[name]

    def encode(self, action: Action) -> str:
        """Render ``action``; raises ``ValueError`` on any schema violation."""
        spec = self._by_name.get(action.name)
        if spec is None:
            raise ValueError(f"unknown action {action.name!r}")
        if len(action.args) != spec.arity:
            raise ValueError(f"{action.name!r} takes {spec.arity} arguments")
        tokens: list[str] = []
        values = iter(action.args)
        for kind in spec.kinds:
            if isinstance(kind, Literal):
                tokens.append(kind.text)
            else:
                tokens.append(_render(kind, next(values)))
        wire = ":".join(tokens)
        if len(wire.encode("ascii")) > MAX_CALLBACK_BYTES:
            raise ValueError(f"encoding of {action!r} exceeds {MAX_CALLBACK_BYTES} bytes")
        return wire

    def decode(self, data: object) -> Action | InvalidCallback:
        """Decode untrusted callback data; never raises."""
        if not isinstance(data, str):
            return InvalidCallback(InvalidReason.NOT_A_STRING)
        if not data:
            return InvalidCallback(InvalidReason.EMPTY)
        if not data.isascii():
            return InvalidCallback(InvalidReason.NOT_ASCII)
        if len(data) > MAX_CALLBACK_BYTES:
            return InvalidCallback(InvalidReason.TOO_LONG)
        tokens = data.split(":")
        if len(tokens) > MAX_TOKENS or any(
            _WIRE_TOKEN_RE.fullmatch(token) is None for token in tokens
        ):
            return InvalidCallback(InvalidReason.BAD_TOKENS)
        for spec in self._by_count[len(tokens)]:
            args: list[int | str] = []
            for kind, token in zip(spec.kinds, tokens, strict=True):
                value = _accepts(kind, token)
                if value is None:
                    break
                if not isinstance(kind, Literal):
                    args.append(value)
            else:
                return Action(spec.name, tuple(args))
        return InvalidCallback(InvalidReason.UNKNOWN_ACTION)

    def is_flow_action(self, action: Action) -> bool:
        """Whether ``action`` belongs to a guided flow (carries a flow token)."""
        return self._by_name[action.name].flow


def _lit(text: str) -> Literal:
    return Literal(text)


PID = IdArg("product_id")
UID = IdArg("user_id")
TOK = FlowTokenArg()


def _product(verb: str, *extra: Kind) -> tuple[Kind, ...]:
    return (_lit("p"), PID, _lit(verb), *extra)


@cache
def currency_choices() -> tuple[str, ...]:
    """Every value the currency position accepts: ISO codes plus two literals."""
    return (*sorted(list_currencies()), *CURRENCY_CHOICE_LITERALS)


def build_registry() -> ActionRegistry:
    """The full closed action registry: menus, lists, product actions, settings,
    guided-flow callbacks and admin.
    """
    specs = [
        ActionSpec("home", (_lit("h"),)),
        ActionSpec("noop", (_lit("noop"),)),
        ActionSpec("stats", (_lit("st"),)),
        ActionSpec("check_all", (_lit("ca"),)),
        ActionSpec("help", (_lit("hp"),)),
        ActionSpec("help.section", (_lit("hp"), Choice("section", HELP_SECTIONS))),
        ActionSpec("back", (_lit("x"), BackArg())),
        ActionSpec("list.page", (_lit("l"), Choice("filter", LIST_FILTERS), IdArg("page"))),
        ActionSpec("list.remove_all", (_lit("l"), _lit("rmall"))),
        ActionSpec("list.remove_all_ok", (_lit("l"), _lit("rmallok"))),
        ActionSpec("product.card", _product("c")),
        ActionSpec("product.check", _product("ck")),
        ActionSpec("product.chart", _product("ch", Choice("period", PERIODS))),
        ActionSpec("product.pause", _product("pa")),
        ActionSpec("product.remove", _product("rm")),
        ActionSpec("product.remove_ok", _product("rmok")),
        ActionSpec("product.edit", _product("ed")),
        ActionSpec("product.reset", _product("rs")),
        ActionSpec("product.reactivate", _product("ra")),
        ActionSpec("product.threshold", _product("th")),
        ActionSpec("product.threshold_any", _product("th", _lit("any"))),
        ActionSpec("product.threshold_default", _product("th", _lit("def"))),
        ActionSpec("product.target", _product("tg")),
        ActionSpec("product.interval", _product("iv")),
        ActionSpec("product.offer_filter", _product("pf", Choice("offer", OFFER_FILTERS))),
        ActionSpec("product.mute", _product("mu", Choice("hours", MUTE_PRESETS))),
        ActionSpec("product.scope_picker", _product("sco")),
        ActionSpec("product.scope", _product("sco", Choice("scope", CARD_SCOPE_CHOICES))),
        ActionSpec(
            "flow.currency",
            (_lit("p"), TOK, _lit("cur"), Choice("currency", currency_choices())),
            flow=True,
        ),
        ActionSpec("flow.scope_picker", (_lit("p"), TOK, _lit("sc")), flow=True),
        ActionSpec(
            "flow.scope",
            (_lit("p"), TOK, _lit("sc"), Choice("scope", FLOW_SCOPE_CHOICES)),
            flow=True,
        ),
        ActionSpec("flow.cancel", (_lit("p"), TOK, _lit("x")), flow=True),
        ActionSpec("settings", (_lit("s"),)),
        ActionSpec("settings.chart_theme", (_lit("s"), _lit("ct"), Choice("theme", THEMES))),
        ActionSpec(
            "settings.language", (_lit("s"), _lit("lang"), Choice("locale", SUPPORTED_LOCALES))
        ),
        ActionSpec("data", (_lit("d"),)),
        ActionSpec("data.export", (_lit("d"), _lit("x"))),
        ActionSpec("data.import", (_lit("d"), _lit("i"))),
        ActionSpec("admin", (_lit("a"),)),
        ActionSpec("admin.users", (_lit("a"), _lit("u"))),
        ActionSpec("admin.add_user", (_lit("a"), _lit("add"))),
        ActionSpec("admin.remove_user", (_lit("a"), _lit("rm"))),
        ActionSpec("admin.remove_user_id", (_lit("a"), _lit("rm"), UID)),
        ActionSpec("admin.nick", (_lit("a"), _lit("nk"))),
        ActionSpec("admin.nick_id", (_lit("a"), _lit("nk"), UID)),
        ActionSpec("admin.interval", (_lit("a"), _lit("iv"))),
        ActionSpec("admin.debug", (_lit("a"), _lit("dbg"))),
    ]
    return ActionRegistry(specs)


REGISTRY: Final = build_registry()


def encode(action: Action) -> str:
    """Encode with the module registry."""
    return REGISTRY.encode(action)


def decode(data: object) -> Action | InvalidCallback:
    """Decode with the module registry; never raises."""
    return REGISTRY.decode(data)
