"""The price grammar: typed decoding, numeric grammar, offer selection.

Three separate stages replace the character-stripping ``parse_price``:

* **Stage 0 — typed decoding** (:func:`decode_price_value`): decides, from the Python
  type of an untrusted value and from where it came from, whether it is a price text
  at all and which grammar reads it. A JSON number, a machine-readable string from a
  structured source (JSON-LD ``price``, microdata ``content``) and a visible text are
  three different kinds (:class:`PriceKind`) with three different grammars.
* **Stage 1 — numeric grammar** (:func:`parse_price_text`): reads one price or rejects
  the text. Nothing is ever "cleaned": signs, exponents, ranges, residual words, a
  second number, mixed grouping and strings whose separator could be read either as a
  thousands or as a decimal separator are rejected, never guessed.
* **Stage 2 — offer selection** (:func:`select_offer`): classifies the offers of one
  product node before decoding any number, and returns one :class:`Money` or an
  :class:`Unreadable` reason.

Every function here is pure and total: untrusted input never raises, it yields ``None``
or :class:`Unreadable`. Only a malformed :class:`PriceContext` (built by trusted code)
raises at construction.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Final

from price_tracker.core.money import (
    ACCEPTED_CURRENCIES,
    MAX_AMOUNT,
    UNKNOWN_CURRENCY_MAX_FRACTION,
    Money,
    currency_precision,
    normalize_currency_code,
    significant_fraction_digits,
)

MAX_TEXT_LENGTH: Final = 64
MAX_INTEGER_DIGITS: Final = 15
MAX_DECODED_VALUE: Final = Decimal(10) ** 12
MAX_LITERAL_FRACTION: Final = 6
INDIAN_GROUPING_CURRENCIES: Final = frozenset({"INR", "PKR", "LKR", "NPR", "BDT"})

DIGIT_SCRIPTS: Final = frozenset({"latin", "arab", "arabext", "fullwidth", "deva"})
GROUPINGS: Final = frozenset({"auto", "western", "indian"})
UNITS: Final = frozenset({"major", "minor"})

UNREADABLE_REASONS: Final = frozenset(
    {"financing_only", "range", "malformed", "currencies_disagree", "no_offer"}
)

# Zero code point of each non-Latin digit script the grammar accepts on declaration.
_SCRIPT_ZERO: Final[Mapping[str, int]] = {
    "arab": 0x0660,
    "arabext": 0x06F0,
    "fullwidth": 0xFF10,
    "deva": 0x0966,
}
# Script-specific separators, translated only when that script is declared.
# The Arabic separators have a single role each; the full-width ones mirror ``,``/``.``.
_ARABIC_DECIMAL: Final = "\u066b"
_ARABIC_GROUPING: Final = "\u066c"
_SCRIPT_SEPARATORS: Final[Mapping[str, Mapping[str, str]]] = {
    "arab": {_ARABIC_DECIMAL: _ARABIC_DECIMAL, _ARABIC_GROUPING: _ARABIC_GROUPING},
    "arabext": {_ARABIC_DECIMAL: _ARABIC_DECIMAL, _ARABIC_GROUPING: _ARABIC_GROUPING},
    "fullwidth": {"\uff0c": ",", "\uff0e": "."},
}

_DUAL_SEPARATORS: Final = frozenset({".", ","})  # decimal or grouping, by context
_DECIMAL_ONLY: Final = frozenset({_ARABIC_DECIMAL})
_GROUPING_ONLY: Final = frozenset(
    {"'", "\u2019", " ", "\u00a0", "\u202f", "\u2009", _ARABIC_GROUPING}
)
_ALL_SEPARATORS: Final = _DUAL_SEPARATORS | _DECIMAL_ONLY | _GROUPING_ONLY

_NO_CENTS_MARKERS: Final = (",-", ".-", ",\u2013", ",\u2014", ".\u2013")

# Currency symbols the grammar strips (a curated subset; the generated table of the
# currency engine replaces it). A symbol maps to every currency that writes it; the
# grammar uses the set only to decide the precision, never to name the currency.
SYMBOLS: Final[Mapping[str, frozenset[str]]] = {
    "€": frozenset({"EUR"}),
    "$": frozenset({"USD", "CAD", "AUD", "NZD", "MXN", "ARS", "CLP", "COP", "SGD", "HKD"}),
    "US$": frozenset({"USD"}),
    "C$": frozenset({"CAD"}),
    "A$": frozenset({"AUD"}),
    "NZ$": frozenset({"NZD"}),
    "S$": frozenset({"SGD"}),
    "HK$": frozenset({"HKD"}),
    "R$": frozenset({"BRL"}),
    "£": frozenset({"GBP"}),
    "¥": frozenset({"JPY", "CNY"}),
    "\uffe5": frozenset({"JPY", "CNY"}),
    "円": frozenset({"JPY"}),
    "元": frozenset({"CNY", "TWD"}),
    "₩": frozenset({"KRW"}),
    "원": frozenset({"KRW"}),
    "₹": frozenset({"INR"}),
    "Rs": frozenset({"INR", "PKR", "LKR", "NPR"}),
    "Rs.": frozenset({"INR", "PKR", "LKR", "NPR"}),
    "zł": frozenset({"PLN"}),
    "kr": frozenset({"SEK", "NOK", "DKK", "ISK"}),
    "kr.": frozenset({"DKK", "ISK"}),
    "₺": frozenset({"TRY"}),
    "TL": frozenset({"TRY"}),
    "₴": frozenset({"UAH"}),
    "₪": frozenset({"ILS"}),
    "฿": frozenset({"THB"}),
    "₫": frozenset({"VND"}),
    "Fr.": frozenset({"CHF"}),
    "Kč": frozenset({"CZK"}),
    "Ft": frozenset({"HUF"}),
}
_SYMBOLS_LONGEST_FIRST: Final = tuple(sorted(SYMBOLS, key=len, reverse=True))
_ISO_TOKEN_AT_START: Final = re.compile(r"([A-Z]{3})(?![A-Za-z])")
_ISO_TOKEN_AT_END: Final = re.compile(r"(?<![A-Za-z])([A-Z]{3})\Z")
_STRUCTURED_RE: Final = re.compile(r"([0-9]{1,15})(?:\.([0-9]{1,6}))?")
_LITERAL_RE: Final = re.compile(r"([0-9]{1,15})(?:\.([0-9]+))?")
_SPLIT_RE: Final = re.compile(r"([^0-9])")


class PriceKind(Enum):
    """Which grammar reads a price text."""

    VISUAL = "visual"  # text shown to a human: locale separators, currency tokens
    STRUCTURED = "structured"  # machine-readable string: '.' is the only separator
    NUMERIC_LITERAL = "numeric_literal"  # a JSON number / Decimal: no separators at all


@dataclass(frozen=True, slots=True)
class PriceText:
    """A decoded price text and the grammar that must read it."""

    text: str
    kind: PriceKind


@dataclass(frozen=True, slots=True)
class PriceContext:
    """What is known before parsing: currency, digit script, grouping, scale."""

    currency: str | None = None  # accepted ISO 4217 code, from the node or storefront
    digits: str = "latin"  # "latin" | "arab" | "arabext" | "fullwidth" | "deva"
    grouping: str = "auto"  # "auto" | "western" | "indian"
    unit: str = "major"  # "major" | "minor" (API sources that document the scale only)

    def __post_init__(self) -> None:
        if self.currency is not None and (
            type(self.currency) is not str or self.currency not in ACCEPTED_CURRENCIES
        ):
            raise ValueError(f"context currency {self.currency!r} is not accepted")
        if self.digits not in DIGIT_SCRIPTS:
            raise ValueError(f"unknown digit script {self.digits!r}")
        if self.grouping not in GROUPINGS:
            raise ValueError(f"unknown grouping {self.grouping!r}")
        if self.unit not in UNITS:
            raise ValueError(f"unknown unit {self.unit!r}")


@dataclass(frozen=True, slots=True)
class Unreadable:
    """A product node whose price cannot be read, and why."""

    reason: str

    def __post_init__(self) -> None:
        if self.reason not in UNREADABLE_REASONS:
            raise ValueError(f"unknown unreadable reason {self.reason!r}")


# --------------------------------------------------------------------------- stage 0


def decode_price_value(value: object, *, structured: bool = False) -> PriceText | None:
    """Decide from its type whether an untrusted value is a price text (stage 0).

    ``structured`` tells where a string came from: ``True`` for a machine-readable
    field (JSON-LD ``price``, microdata ``content``), ``False`` for visible text.
    Booleans, containers, ``None`` and non-finite or non-positive numbers are never
    prices and are never ``str()``-coerced.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not 1 <= len(text) <= MAX_TEXT_LENGTH:
            return None
        return PriceText(text, PriceKind.STRUCTURED if structured else PriceKind.VISUAL)
    if isinstance(value, int):
        if 0 < value < MAX_DECODED_VALUE:
            return PriceText(str(value), PriceKind.NUMERIC_LITERAL)
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if 0 < value < MAX_DECODED_VALUE:
            return PriceText(repr(value), PriceKind.NUMERIC_LITERAL)
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        if 0 < value < MAX_DECODED_VALUE:
            digits = len(value.as_tuple().digits)
            exponent = value.as_tuple().exponent
            assert isinstance(exponent, int)
            width = (
                digits + exponent
                if exponent >= 0
                else max(digits, -exponent) + 1 + int(digits <= -exponent)
            )
            if width > MAX_TEXT_LENGTH:
                return None
            return PriceText(format(value, "f"), PriceKind.NUMERIC_LITERAL)
        return None
    return None


# --------------------------------------------------------------------------- stage 1


@dataclass(frozen=True, slots=True)
class _CurrencyEvidence:
    """What the text and the context say about the currency."""

    currency: str | None  # a single known currency, if any
    precision: int | None  # known minor-unit digits, if determinable


def _translate_digits(text: str, script: str) -> str | None:
    """Map the declared digit script to ASCII; reject any other non-ASCII digit."""
    zero = _SCRIPT_ZERO.get(script)
    separators = _SCRIPT_SEPARATORS.get(script, {})
    out: list[str] = []
    saw_ascii = False
    saw_script = False
    for char in text:
        if "0" <= char <= "9":
            saw_ascii = True
            out.append(char)
            continue
        if char in separators:
            out.append(separators[char])
            continue
        if char in {_ARABIC_DECIMAL, _ARABIC_GROUPING, "\uff0c", "\uff0e"}:
            return None
        if unicodedata.decimal(char, None) is not None:
            if zero is None or not zero <= ord(char) <= zero + 9:
                return None
            saw_script = True
            out.append(chr(ord("0") + ord(char) - zero))
            continue
        out.append(char)
    if saw_ascii and saw_script:
        return None  # one number, one digit script
    return "".join(out)


def _is_letter(char: str) -> bool:
    return unicodedata.category(char).startswith("L")


def _strip_currency_token(text: str) -> tuple[str, str | None, frozenset[str] | None]:
    """Remove at most one currency token, at the start or at the end.

    Returns ``(residue, iso_code, symbol_candidates)``. A symbol glued to a letter is
    not a token. A second token is left in the residue, where it fails the grammar.
    """
    match = _ISO_TOKEN_AT_START.match(text)
    if match and match.group(1) in ACCEPTED_CURRENCIES:
        return text[match.end() :].strip(), match.group(1), None
    match = _ISO_TOKEN_AT_END.search(text)
    if match and match.group(1) in ACCEPTED_CURRENCIES:
        return text[: match.start()].strip(), match.group(1), None
    for symbol in _SYMBOLS_LONGEST_FIRST:
        if text.startswith(symbol):
            rest = text[len(symbol) :]
            if rest and _is_letter(symbol[-1]) and _is_letter(rest[0]):
                continue
            return rest.strip(), None, SYMBOLS[symbol]
        if text.endswith(symbol):
            head = text[: -len(symbol)]
            if head and _is_letter(symbol[0]) and _is_letter(head[-1]):
                continue
            return head.strip(), None, SYMBOLS[symbol]
    return text, None, None


def _currency_evidence(
    ctx: PriceContext, iso: str | None, candidates: frozenset[str] | None
) -> _CurrencyEvidence | None:
    """Combine context and in-text token; ``None`` when they contradict each other."""
    if ctx.currency is not None:
        if iso is not None and iso != ctx.currency:
            return None
        if candidates is not None and ctx.currency not in candidates:
            return None
        return _CurrencyEvidence(ctx.currency, currency_precision(ctx.currency))
    if iso is not None:
        return _CurrencyEvidence(iso, currency_precision(iso))
    if candidates is not None:
        accepted = sorted(c for c in candidates if c in ACCEPTED_CURRENCIES)
        precisions = {currency_precision(c) for c in accepted}
        single = accepted[0] if len(accepted) == 1 else None
        return _CurrencyEvidence(single, precisions.pop() if len(precisions) == 1 else None)
    return _CurrencyEvidence(None, None)


def _western_groups(groups: list[str]) -> bool:
    return 1 <= len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:])


def _indian_groups(groups: list[str]) -> bool:
    if len(groups) < 2 or len(groups[-1]) != 3 or not 1 <= len(groups[0]) <= 2:
        return False
    return all(len(g) == 2 for g in groups[1:-1])


def _parse_visual(
    residue: str, precision: int | None, indian_allowed: bool, no_cents: str | None
) -> tuple[str, str] | None:
    """Split a visual residue into ``(integer digits, fraction digits)`` or reject."""
    parts = _SPLIT_RE.split(residue)
    runs = parts[0::2]
    seps = parts[1::2]
    if any(run == "" for run in runs) or any(sep not in _ALL_SEPARATORS for sep in seps):
        return None
    decimal: str | None = None
    if seps:
        last = seps[-1]
        if no_cents is not None:
            if no_cents in seps or any(sep in _DECIMAL_ONLY for sep in seps):
                return None
        elif last in _DECIMAL_ONLY:
            decimal = last
        elif last in _DUAL_SEPARATORS and seps.count(last) == 1:
            if len(seps) > 1:
                decimal = last  # other separators group: the last one is the decimal
            else:
                fraction_len = len(runs[-1])
                if fraction_len <= 2:
                    decimal = last
                elif fraction_len == 3:
                    if precision == 3:
                        decimal = last
                    elif precision is None:
                        if 1 <= len(runs[0]) <= 3:
                            return None  # thousands or decimals: ambiguous by construction
                        decimal = last
                    # precision 0..2: a lone separator before three digits groups
                else:
                    return None
    if decimal is not None:
        groups, fraction, group_seps = runs[:-1], runs[-1], seps[:-1]
    else:
        groups, fraction, group_seps = runs, "", seps
    if group_seps:
        chars = set(group_seps)
        if len(chars) != 1:
            return None  # mixed grouping characters
        group_char = group_seps[0]
        if group_char in _DECIMAL_ONLY or group_char == decimal:
            return None
        if precision == 3 and group_char in _DUAL_SEPARATORS:
            return None  # a three-decimal currency never groups with '.' or ','
        western = _western_groups(groups)
        indian = indian_allowed and group_char == "," and _indian_groups(groups)
        if not (western or indian):
            return None
    return "".join(groups), fraction


def _finish(
    integer: str, fraction: str, evidence: _CurrencyEvidence, ctx: PriceContext, kind: PriceKind
) -> Decimal | None:
    """Apply digit-count, precision, scale and bounds rules; build the Decimal."""
    if not integer or len(integer.lstrip("0") or "0") > MAX_INTEGER_DIGITS:
        return None
    if len(integer) > MAX_INTEGER_DIGITS + 3:
        return None
    try:
        value = Decimal(f"{integer}.{fraction}" if fraction else integer)
    except InvalidOperation:  # pragma: no cover - digits only by construction
        return None
    if kind is PriceKind.NUMERIC_LITERAL:
        fraction_digits = significant_fraction_digits(value) if value else 0
    else:
        fraction_digits = len(fraction)
    if ctx.unit == "minor":
        if kind is PriceKind.VISUAL or evidence.currency is None or fraction_digits:
            return None
        value = value.scaleb(-currency_precision(evidence.currency))
        limit = currency_precision(evidence.currency)
    else:
        limit = UNKNOWN_CURRENCY_MAX_FRACTION if evidence.precision is None else evidence.precision
        if fraction_digits > limit:
            return None
    if not value.is_finite() or not (0 < value <= MAX_AMOUNT):
        return None
    return value


def parse_price_text(text: PriceText | str, ctx: PriceContext | None = None) -> Decimal | None:
    """Read one price from a decoded text, or return ``None`` (stage 1).

    A plain ``str`` is read as :attr:`PriceKind.VISUAL`. The result is always a finite
    ``Decimal`` in ``(0, 10**9]``; any text that is not exactly one price is rejected.
    """
    context = ctx if ctx is not None else PriceContext()
    if isinstance(text, str):
        price_text = PriceText(text.strip(), PriceKind.VISUAL)
    elif isinstance(text, PriceText) and isinstance(text.text, str):
        price_text = text
    else:
        return None
    raw = price_text.text
    if not 1 <= len(raw) <= MAX_TEXT_LENGTH:
        return None
    kind = price_text.kind
    if kind is PriceKind.NUMERIC_LITERAL:
        match = _LITERAL_RE.fullmatch(raw)
        if match is None:
            return None
        fraction = (match.group(2) or "").rstrip("0")
        if len(fraction) > MAX_LITERAL_FRACTION:
            return None
        evidence = _currency_evidence(context, None, None)
        if evidence is None:  # pragma: no cover - no token, cannot contradict
            return None
        return _finish(match.group(1), fraction, evidence, context, kind)
    if kind is PriceKind.STRUCTURED:
        match = _STRUCTURED_RE.fullmatch(raw)
        if match is None:
            return None
        integer, fraction = match.group(1), match.group(2) or ""
        evidence = _currency_evidence(context, None, None)
        if evidence is None:  # pragma: no cover - no token, cannot contradict
            return None
        if evidence.precision is None and len(fraction) == 3 and len(integer) <= 3:
            return None  # "12.345" without a currency: thousands or decimals
        return _finish(integer, fraction, evidence, context, kind)
    if kind is not PriceKind.VISUAL:
        return None
    translated = _translate_digits(raw, context.digits)
    if translated is None:
        return None
    residue, iso, candidates = _strip_currency_token(translated.strip())
    evidence = _currency_evidence(context, iso, candidates)
    if evidence is None:
        return None
    no_cents: str | None = None
    for marker in _NO_CENTS_MARKERS:
        if residue.endswith(marker):
            residue = residue[: -len(marker)]
            no_cents = marker[0]
            break
    indian_allowed = context.grouping == "indian" or (
        context.grouping == "auto" and evidence.currency in INDIAN_GROUPING_CURRENCIES
    )
    split = _parse_visual(residue, evidence.precision, indian_allowed, no_cents)
    if split is None:
        return None
    integer, fraction = split
    return _finish(integer, fraction, evidence, context, kind)


def parse_price_value(
    value: object, ctx: PriceContext | None = None, *, structured: bool = False
) -> Decimal | None:
    """Stage 0 followed by stage 1: never raises, never coerces a container."""
    decoded = decode_price_value(value, structured=structured)
    if decoded is None:
        return None
    return parse_price_text(decoded, ctx)


# --------------------------------------------------------------------------- stage 2

_SCHEMA_PREFIXES: Final = ("https://schema.org/", "http://schema.org/")
_FINANCING_TEXT: Final = re.compile(
    r"/\s*mo\b|per\s+month|\bx\s*\d+\b|\brate\b|\braten\b|mensualit|al\s+mese|\bcuota"
    r"|\bmonthly\s+payments?\b|\binstalments?\b|\binstallments?\b",
    re.IGNORECASE,
)
_STRIKE_PRICE_TYPES: Final = frozenset(
    {"listprice", "strikethroughprice", "msrp", "srp", "regularprice", "compareatprice"}
)
_TEXT_FIELDS: Final = ("name", "description", "category")


def _schema_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    for prefix in _SCHEMA_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :].casefold()
    return text.casefold()


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _classify_offer(offer: Mapping[str, object]) -> str | None:
    """Return the exclusion class of an offer, or ``None`` when it is a candidate."""
    types = {_schema_name(t) for t in _as_list(offer.get("@type"))}
    if "aggregateoffer" in types or "lowPrice" in offer or "highPrice" in offer:
        return "range"
    if "paymentchargespecification" in types:
        return "financing_only"
    if "billingDuration" in offer or "billingIncrement" in offer:
        return "financing_only"
    if "referenceQuantity" in offer:
        return "unit_price"
    blob = " ".join(str(offer.get(k, "")) for k in _TEXT_FIELDS if isinstance(offer.get(k), str))
    for spec in _as_list(offer.get("priceSpecification")):
        if not isinstance(spec, Mapping):
            return "malformed"
        spec_types = {_schema_name(t) for t in _as_list(spec.get("@type"))}
        if "paymentchargespecification" in spec_types:
            return "financing_only"
        if "billingDuration" in spec or "billingIncrement" in spec:
            return "financing_only"
        if spec.get("referenceQuantity") is not None:
            return "unit_price"
        if _schema_name(spec.get("priceType")) in _STRIKE_PRICE_TYPES:
            return "strikethrough"
        for key in ("name", "description"):
            text = spec.get(key)
            if isinstance(text, str):
                blob += " " + text
    if _schema_name(offer.get("priceType")) in _STRIKE_PRICE_TYPES:
        return "strikethrough"
    if _FINANCING_TEXT.search(blob):
        return "financing_only"
    return None


def select_offer(offers: object, ctx: PriceContext | None = None) -> Money | Unreadable:
    """Pick the node's single price from its ``offers`` value (stage 2).

    Financing, unit-price, range and strike-through offers are excluded before any
    number is decoded; a candidate is an offer whose single ``price`` decodes as a
    structured value under its own ``priceCurrency``. Candidates in different
    currencies make the node unreadable (never order-dependent); otherwise the highest
    amount is the node's price.
    """
    context = ctx if ctx is not None else PriceContext()
    if offers is None:
        return Unreadable("no_offer")
    if isinstance(offers, Mapping):
        offer_list: list[object] = [offers]
    elif isinstance(offers, list):
        offer_list = list(offers)
    else:
        return Unreadable("malformed")
    if not offer_list:
        return Unreadable("no_offer")

    candidates: list[Money] = []
    exclusions: list[str] = []
    for offer in offer_list:
        if not isinstance(offer, Mapping):
            exclusions.append("malformed")
            continue
        excluded = _classify_offer(offer)
        if excluded is not None:
            exclusions.append(excluded)
            continue
        raw_currency = offer.get("priceCurrency")
        currency = normalize_currency_code(raw_currency)
        if raw_currency is not None and currency is None:
            exclusions.append("malformed")  # a declared currency we cannot read
            continue
        if currency is None:
            currency = context.currency
        elif context.currency is not None and currency != context.currency:
            exclusions.append("malformed")
            continue
        offer_ctx = PriceContext(
            currency=currency, digits=context.digits, grouping=context.grouping, unit=context.unit
        )
        amount = parse_price_value(offer.get("price"), offer_ctx, structured=True)
        if amount is None:
            exclusions.append("malformed")
            continue
        candidates.append(Money(amount, currency))

    if candidates:
        currencies = {m.currency for m in candidates if m.currency is not None}
        if len(currencies) > 1:
            return Unreadable("currencies_disagree")
        unified = currencies.pop() if currencies else None
        if unified is not None and any(
            significant_fraction_digits(m.amount) > currency_precision(unified) for m in candidates
        ):
            return Unreadable("malformed")  # a currency-less offer too precise for the node
        best = max(candidates, key=lambda m: (m.amount, str(m.amount)))
        return Money(best.amount, unified)
    if exclusions and all(e == "financing_only" for e in exclusions):
        return Unreadable("financing_only")
    if exclusions and all(e == "range" for e in exclusions):
        return Unreadable("range")
    return Unreadable("malformed")
