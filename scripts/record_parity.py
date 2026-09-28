"""Freeze the behaviour of the old ``parse_price`` / ``detect_currency`` parsers.

This CLI builds and checks the parity corpus and the frozen snapshots recorded in
``tests/parity/``. It never regenerates a frozen row from a new implementation of the
functions it freezes: once a function is frozen, the only way to change its recorded
rows is to declare it ``wired`` in the manifest and re-derive the exceptions from a
live diff.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import pkgutil
import re
import sys
import unicodedata
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from price_tracker.core.money import ACCEPTED_CURRENCIES, currency_precision

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
BASE_COMMIT = "83a91f68dc9e9b3483a6c11d232b85946870188c"
# --- Section 3.2: corpus constants (curated, normative content) ---

TEST_LITERALS: tuple[str | None, ...] = (
    "29,99 €",
    "€29.99",
    "1.299,99",
    "1,299.99",
    "5'250.00",
    "CHF 5,250,00",
    "$1,234",
    "EUR 29,99",
    "1.299",
    "2.499",
    "12.999",
    "1.234",
    "1.299 €",
    "1.234.567",
    "12.345.678",
    "1,234,567",
    "$12,345,678",
    "349,-",
    "799,-",
    "1.299,-",
    "1 299,00 zł",
    "129,00 zł",
    "1 299 Kč",
    "1 299,00 kr",
    "0.999",
    "0,99",
    "1 234,56",
    "$1,29999",
    "1,29999",
    "1.29999",
    "1234,5678",
    "",
    "not a price",
    None,
    "EUR",
    "CHF ",
    "1.2.3.4",
    "$29.99",
    "£29.99",
    "¥1000",
    "CHF 25.00",
)

GRAMMAR_ROWS: tuple[str, ...] = (
    "1.234,56",
    "1,234.56",
    "1.234.567,89",
    "1 234,56",
    "1\xa0234,56",
    "1 234,56 €",
    "1 234.56",
    "1'234.56",
    "1’234.56",
    "1 234 567",
    "1,5",
    "19,-",
    "19,–",
    "1.234,-",
    "£1,234.56",
    "BHD 1.234",
    "KWD 12.345",
    "R$ 1.234,56",
    "1 234,56 zł",
    "EUR 12,50",
    "12.50 USD",
    "1234.567",
    "1000000000",
    "1e3",
    "-5",
    "−5",
    "+5",
    "0",
    "0,00",
    "NaN",
    "Infinity",
    "12 34",
    "1,23,45",
    "12,",
    "€ -5",
    "EUR 10–20",
    "Was 20 now 10",
    "10 €/kg",
    "€ 10 USD",
    "1,2345",
    "1'234,567.89",
    "11111111111111111111111111111111111111111111111111111111111111111",
    "1111111111111111",
    "1 234\xa0567",
    "1..234",
    ",99",
    "1.234,567,890",
    "1000000001",
    "12.345",
    "12,345",
    "999,999",
    "$12.345",
    "kr 1.234",
    "¥1,234",
    "-12,50 €",
    "5-",
    "(5)",
    "krone 10",
    "EURO 10",
    "EUR10",
    "USD 10",
    "₹10",
    "kr 10",
)

HOSTILE: tuple[str, ...] = (
    "1​299,00",
    "‮1299",
    "12\xa0€",
    "€\xa012,50",
    "1,299.99 USD",
    "USD1299",
    "1299.99 usd",
    "99.99€",
    "99.99 EUR incl. VAT",
    "from 12,99",
    "12,99 – 24,99",
    "12.99*",
    "*12.99",
    "12,99 €/Stk.",
    "ab 12,99 €",
    "1.234.567.890",
    "1,234,567,890.12",
    "0.1",
    "0,1",
    "00012",
    "12.",
    ".12",
    "1,",
    "١٢٣٤",
    "１２３４",
    "१२३४",
    "12٫50",
    "1.234,56\n",
    " 29,99 ",
    "\t29,99",
    "29,99\r\n€",
    "NUL\x00 12",
    "12 34 56",
    "1 2 3",
    "kr",
    "€",
    "$",
    "SEK",
    "sek 10",
    "10 Kr",
    "10 KR",
    "dkk 10",
    "1299.00",
    "1299",
    "129900",
    "12.5",
    "12.50",
    "9,99",
    "1E+3",
    "1e-3",
    "1.0e3",
    "1_000",
    "1 000",
    "1e-05",
    "1e+16",
    "1299.0",
    "1500.0",
    "1234567.0",
    "1 USD = 0.9 EUR",
    "kr. 89",
    "26,72 kr.",
    "Fr. 70,205,780",
    "10 nok",
    "Kr 10",
    "10 kr",
    "10 NOK",
    "100 DKK",
    "usd",
    "EUR 10 USD",
    "  ",
    "\xa0",
    "€ €",
    "1.234,56 €  ",
    "0,00 €",
    "0",
    "00",
    "0.0",
    "999999999",
    "1000000000",
    "1.000.000.000",
    "1,000,000,000.00",
)

URL_SHAPED: tuple[str, ...] = (
    "http://example.test/p/1",
    "https://example.com/gbp/1",
    "https://example.invalid/p/1",
    "https://example.net/eur/1",
    "https://example.org/p/1?currency=usd",
    "https://shop.example/kr/1",
    "https://shop.example/p/1",
    "https://www.example.com/products/sekret-1",
    "https://www.example.org/kreditkarte/1",
)

# --- Section 3.3: generator (normative, transcribed verbatim) ---

SEED = "sp2-pr0-parity-v1"
TOKENS = (
    "€",
    "$",
    "£",
    "¥",
    "zł",
    "Kč",
    "kr",
    "kr.",
    "CHF",
    "EUR",
    "USD",
    "GBP",
    "JPY",
    "SEK",
    "NOK",
    "DKK",
    "PLN",
    "CZK",
    "Fr.",
    "R$",
)
WORDS_BEFORE = ("from", "ab", "UVP", "Was", "nur", "solo")
WORDS_AFTER = ("/kg", "/mo", "*", "incl. VAT", "– 24,99", "each")
FORMATS = {
    0: (".", ","),
    1: (",", "."),
    2: ("'", "."),
    3: (" ", ","),
    4: ("\xa0", ","),
    5: ("", "."),
}
GLUES = ("", " ", "\xa0")


def _draw(i: int, n: int, slot: int) -> int:
    digest = hashlib.sha256(f"{SEED}:{i}:{slot}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % n


def _digits(i: int, slot: int, k: int) -> str:
    return "".join(str(_draw(i, 10, slot * 100 + j)) for j in range(k))


def generate(count: int) -> list[str]:
    out: list[str] = []
    for i in range(count):
        head_len = 1 + _draw(i, 3, 1)
        groups = _draw(i, 3, 2)
        if head_len > 1:
            head = str(1 + _draw(i, 9, 3)) + _digits(i, 4, head_len - 1)
        else:
            head = str(_draw(i, 10, 3))
        grp = [_digits(i, 5 + g, 3) for g in range(groups)]
        frac_kind = _draw(i, 6, 8)
        frac = {
            0: "",
            1: "",
            2: _digits(i, 9, 2),
            3: _digits(i, 9, 2),
            4: _digits(i, 9, 1),
            5: _digits(i, 9, 3),
        }[frac_kind]
        fmt = _draw(i, 6, 10)
        gsep, dsep = FORMATS[fmt]
        body = gsep.join([head, *grp])
        if frac:
            body += dsep + frac
        elif _draw(i, 5, 11) == 0 and fmt in (0, 3, 4):
            body += ",-"
        tok_kind = _draw(i, 10, 12)
        if tok_kind >= 3:
            tok = TOKENS[_draw(i, len(TOKENS), 13)]
            glue = GLUES[_draw(i, 3, 14)]
            body = f"{body}{glue}{tok}" if tok_kind <= 6 else f"{tok}{glue}{body}"
        mut = _draw(i, 20, 15)
        if mut == 0:
            body = WORDS_BEFORE[_draw(i, len(WORDS_BEFORE), 16)] + " " + body
        elif mut == 1:
            body = body + " " + WORDS_AFTER[_draw(i, len(WORDS_AFTER), 16)]
        out.append(body)
    return out


# --- Section 3.5: exceptions classifier (normative, transcribed verbatim) ---

KNOWN_TOKENS = sorted(
    set(TOKENS) | {"Rs", "Rs.", "TL", "Ft", "US$", "₹", "₺", "BHD", "KWD"},
    key=len,
    reverse=True,
)
KNOWN_TOKEN_RE = re.compile("|".join(re.escape(t) for t in KNOWN_TOKENS), re.IGNORECASE)
EXACT_TOKEN_RE = re.compile("|".join(re.escape(t) for t in KNOWN_TOKENS))
SEPS = ".,'\u2019 \xa0\u202f\u2009"
GROUP_SEPS = "'\u2019 \xa0\u202f\u2009.,"
FORMAT_CHARS = frozenset(
    "\u200b\u200c\u200d\u202a\u202b\u202c\u202d\u202e\ufeff\u2066\u2067\u2068\u2069"
)
NO_CENTS = r"(?:,-|\.-|,–)?"
# one grouping character repeated, then at most one decimal separator that differs from it
WELL_FORMED_RE = re.compile(
    rf"(?:[0-9]{{1,3}}(?P<g>[{re.escape(GROUP_SEPS)}])[0-9]{{3}}(?:(?P=g)[0-9]{{3}})*|[0-9]+)"
    rf"(?:(?!(?P=g))[.,][0-9]{{1,3}})?{NO_CENTS}"
)
SECOND_NUMBER_RE = re.compile(
    r"[0-9]\s*[–-]\s*[0-9]|[0-9] +[0-9]{1,2}(?![0-9])|(?<![0-9])[0-9]{1,2} +[0-9]"
)
THREE_DIGIT_AMBIGUITY_RE = re.compile(r"0*[1-9]?[0-9]{0,2}[.,][0-9]{3}")
SEPARATORS_ONLY_RE = re.compile(f"[0-9{re.escape(SEPS)}]+{NO_CENTS}")


def _strip_tokens(s: str) -> str:
    return KNOWN_TOKEN_RE.sub(" ", s)


def _skeleton(s: str) -> str:
    return _strip_tokens(s).strip()


def _well_formed(s: str) -> bool:
    return WELL_FORMED_RE.fullmatch(_skeleton(s)) is not None


def _iso_codes(s: str) -> list[str]:
    return [code for code in re.findall(r"[A-Z]{3}", s) if code in ACCEPTED_CURRENCIES]


def _fraction_len(s: str) -> int:
    match = re.search(r"[.,]([0-9]{1,3})$", _skeleton(s))
    return len(match.group(1)) if match else 0


def _has_control_or_format(s: str) -> bool:
    return any(ord(c) < 32 or c in FORMAT_CHARS or 0xD800 <= ord(c) <= 0xDFFF for c in s)


def _has_sign_or_exponent(s: str) -> bool:
    signed = re.search(r"[-+−(]|[0-9][eE][-+]?[0-9]|_", s) is not None
    return signed and re.search(r"[.,][-–]", s) is None


def _has_residual_words(s: str) -> bool:
    return (
        re.search(r"[^\W\d_]{2,}", _strip_tokens(s)) is not None
        or re.search(r"[*/=]", s) is not None
    )


LossPredicate = Callable[[str, str], bool]
ValuePredicate = Callable[[str], bool]

LOSS_CLASSES: tuple[tuple[str, LossPredicate], ...] = (
    ("zero", lambda s, o: Decimal(o) == 0),
    ("over_length", lambda s, o: len(s.strip()) > 64),
    ("over_bound", lambda s, o: Decimal(o) > Decimal(10) ** 9),
    ("control_or_format_char", lambda s, o: _has_control_or_format(s)),
    ("non_latin_digits", lambda s, o: any(c.isdigit() and not c.isascii() for c in s)),
    ("sign_or_exponent", lambda s, o: _has_sign_or_exponent(s)),
    ("residual_words", lambda s, o: _has_residual_words(s)),
    ("two_tokens", lambda s, o: len(KNOWN_TOKEN_RE.findall(s)) >= 2),
    ("token_case", lambda s, o: re.search(r"[^\W\d_]{2,}", EXACT_TOKEN_RE.sub(" ", s)) is not None),
    (
        "precision_exceeded",
        lambda s, o: any(_fraction_len(s) > currency_precision(c) for c in _iso_codes(s)),
    ),
    (
        "second_number",
        lambda s, o: not _well_formed(s) and SECOND_NUMBER_RE.search(_skeleton(s)) is not None,
    ),
    (
        "three_digit_ambiguity",
        lambda s, o: THREE_DIGIT_AMBIGUITY_RE.fullmatch(_skeleton(s)) is not None,
    ),
    ("three_decimals_rejected", lambda s, o: re.search(r"[.,][0-9]{3}$", _skeleton(s)) is not None),
    ("dangling_separator", lambda s, o: re.search(r"^[.,]|[.,]$", _skeleton(s)) is not None),
    (
        "invalid_grouping",
        lambda s, o: not _well_formed(s) and SEPARATORS_ONLY_RE.fullmatch(_skeleton(s)) is not None,
    ),
)
VALUE_CLASSES: tuple[tuple[str, ValuePredicate], ...] = (
    ("token_with_dot", lambda s: re.search(r"(kr|Fr|Rs)\.", s) is not None),
    ("three_decimals", lambda s: re.search(r"[.,][0-9]{3}$", _skeleton(s)) is not None),
)


def classify(s: str | None, old: str | None, new: str | None) -> tuple[str, str]:
    """Name a divergence: ``(kind, class)``; the first true predicate wins."""
    if old is not None and new is not None:
        kind, fallback = "value", "value_other"
    elif old is None:
        kind, fallback = "gain", "gain_other"
    else:
        kind, fallback = "loss", "loss_other"
    if s is None:
        return kind, fallback
    if kind in ("value", "gain"):
        for name, value_predicate in VALUE_CLASSES:
            if value_predicate(s):
                return kind, name
        return kind, fallback
    assert old is not None
    for name, loss_predicate in LOSS_CLASSES:
        if loss_predicate(s, old):
            return kind, name
    return kind, fallback


ALL_CLASSES: dict[str, frozenset[str]] = {
    "loss": frozenset({name for name, _ in LOSS_CLASSES}) | {"loss_other"},
    "value": frozenset({name for name, _ in VALUE_CLASSES}) | {"value_other"},
    "gain": frozenset({name for name, _ in VALUE_CLASSES}) | {"gain_other"},
}


# --- Section 3.4: format errors, loaders, writer ---


class ParityFormatError(Exception):
    """Raised by the load_* functions on any deviation from the pinned format.

    The loaders never repair a malformed file: every deviation raises, and the
    caller decides what to do (usually: fail the test that loaded it).
    """

    def __init__(self, code: str, detail: str = "") -> None:
        message = f"{code}: {detail}" if detail else code
        super().__init__(message)
        self.code = code
        self.detail = detail


KNOWN_FUNCTIONS = ("parse_price", "detect_currency")

CORPUS_KEYS = {"schema", "count", "harvested", "curated", "generated"}
GENERATED_KEYS = {"seed", "requested", "inputs"}
MANIFEST_KEYS = {"schema", "base_commit", "functions"}
FUNCTION_KEYS = {"frozen", "source_sha256", "subject", "wired"}
FROZEN_KEYS = {"schema", "function", "count", "rows"}
FROZEN_ROW_KEYS = {"input", "old"}
EXCEPTIONS_KEYS = {"schema", "function", "subject", "count", "rows"}
EXCEPTIONS_ROW_KEYS = {"input", "old", "new", "kind", "class", "note"}

_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Cn", "Zs", "Zl", "Zp"})

_SOURCE_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CURRENCY_CODE_RE = re.compile(r"[A-Z]{3}")


def sort_key(x: str | None) -> tuple[bool, str]:
    """The corpus ordering key of P10: ``None`` first, then strings by code point."""
    return (x is not None, x or "")


def _read_json(path: Path, where: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ParityFormatError(f"{where}_file_not_utf8", str(exc)) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParityFormatError(f"{where}_invalid_json", str(exc)) from exc


def _require_object(value: Any, where: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ParityFormatError(f"{where}_not_object", repr(value))
    return value


def _require_keys(obj: dict[str, Any], expected: set[str], where: str) -> None:
    actual = set(obj.keys())
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ParityFormatError(f"{where}_keys", f"missing={missing} extra={extra}")


def _require_int(value: Any, where: str) -> int:
    if type(value) is not int:
        raise ParityFormatError(f"{where}_not_int", repr(value))
    return value


def _require_bool(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise ParityFormatError(f"{where}_not_bool", repr(value))
    return value


def _require_list(value: Any, where: str) -> list[Any]:
    if type(value) is not list:
        raise ParityFormatError(f"{where}_not_list", repr(value))
    return value


def _check_utf8(s: str, where: str) -> None:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ParityFormatError(f"{where}_not_utf8", repr(s)) from exc


def _require_str_or_none(value: Any, where: str, *, allow_none: bool = True) -> str | None:
    if value is None:
        if not allow_none:
            raise ParityFormatError(f"{where}_none_not_allowed")
        return None
    if type(value) is not str:
        raise ParityFormatError(f"{where}_not_str_or_none", repr(value))
    _check_utf8(value, where)
    return value


def _check_ordered_inputs(items: list[str | None], where: str) -> None:
    if items != sorted(items, key=sort_key):
        raise ParityFormatError(f"{where}_unordered")
    if len(set(items)) != len(items):
        raise ParityFormatError(f"{where}_duplicate")


def resolve(mod_attr: Any, where: str) -> Callable[..., Any]:
    """Resolve a ``module:attribute`` string. Never returns on failure: it raises."""
    if type(mod_attr) is not str or ":" not in mod_attr:
        raise ParityFormatError(f"{where}_malformed", repr(mod_attr))
    mod_name, _, attr_name = mod_attr.partition(":")
    if not mod_name or not attr_name:
        raise ParityFormatError(f"{where}_malformed", repr(mod_attr))
    try:
        module = importlib.import_module(mod_name)
    except ImportError as exc:
        raise ParityFormatError(f"{where}_unresolvable", mod_attr) from exc
    if not hasattr(module, attr_name):
        raise ParityFormatError(f"{where}_unresolvable", mod_attr)
    obj = getattr(module, attr_name)
    if not callable(obj):
        raise ParityFormatError(f"{where}_not_callable", mod_attr)
    return obj  # type: ignore[no-any-return]


def _is_canonical_decimal_string(s: str) -> bool:
    """The five conditions of Sec. 3.4 that a frozen ``parse_price`` value must meet."""
    if "E" in s:
        return False
    try:
        d = Decimal(s)
    except InvalidOperation:
        return False
    if not d.is_finite():
        return False
    if d.is_signed():
        return False
    return s == str(d)


def _is_canonical_currency_code(s: str) -> bool:
    return _CURRENCY_CODE_RE.fullmatch(s) is not None


def value_ok_for_function(function: str, value: str) -> bool:
    if function == "parse_price":
        return _is_canonical_decimal_string(value)
    if function == "detect_currency":
        return _is_canonical_currency_code(value)
    raise ParityFormatError("unknown_function", function)


def serialize(value: Decimal | str | None) -> str | None:
    """``None -> None``, ``Decimal -> str(d)``, ``str -> s`` (Sec. 3.5)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return value
    raise TypeError(f"cannot serialize {value!r}")


def _dedup_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def load_corpus(path: Path) -> dict[str, Any]:
    data = _require_object(_read_json(path, "corpus"), "corpus")
    _require_keys(data, CORPUS_KEYS, "corpus")
    if _require_int(data["schema"], "corpus_schema") != 1:
        raise ParityFormatError("corpus_schema_version", repr(data["schema"]))
    count = _require_int(data["count"], "corpus_count")
    harvested_raw = _require_list(data["harvested"], "corpus_harvested")
    harvested = [_require_str_or_none(x, "corpus_harvested_item") for x in harvested_raw]
    _check_ordered_inputs(harvested, "corpus_harvested")
    curated_raw = _require_list(data["curated"], "corpus_curated")
    curated = [
        _require_str_or_none(x, "corpus_curated_item", allow_none=False) for x in curated_raw
    ]
    _check_ordered_inputs(curated, "corpus_curated")
    generated_obj = _require_object(data["generated"], "corpus_generated")
    _require_keys(generated_obj, GENERATED_KEYS, "corpus_generated")
    seed = generated_obj["seed"]
    if type(seed) is not str:
        raise ParityFormatError("corpus_generated_seed_not_str", repr(seed))
    requested = _require_int(generated_obj["requested"], "corpus_generated_requested")
    generated_inputs_raw = _require_list(generated_obj["inputs"], "corpus_generated_inputs")
    generated_inputs = [
        _require_str_or_none(x, "corpus_generated_inputs_item", allow_none=False)
        for x in generated_inputs_raw
    ]
    _check_ordered_inputs(generated_inputs, "corpus_generated_inputs")

    total = len(harvested) + len(curated) + len(generated_inputs)
    if count != total or count == 0:
        raise ParityFormatError("corpus_count_mismatch", f"count={count} total={total}")

    h_set, c_set, g_set = set(harvested), set(curated), set(generated_inputs)
    if (h_set & c_set) or (h_set & g_set) or (c_set & g_set):
        raise ParityFormatError("corpus_sections_overlap")

    dedup = _dedup_preserve_order(generate(requested))
    expected_generated = sorted(
        (x for x in dedup if x not in h_set and x not in c_set), key=sort_key
    )
    if generated_inputs != expected_generated:
        raise ParityFormatError("corpus_generated_not_reproducible")

    return {
        "schema": 1,
        "count": count,
        "harvested": harvested,
        "curated": curated,
        "generated": {"seed": seed, "requested": requested, "inputs": generated_inputs},
    }


def load_manifest(path: Path) -> dict[str, Any]:
    data = _require_object(_read_json(path, "manifest"), "manifest")
    _require_keys(data, MANIFEST_KEYS, "manifest")
    if _require_int(data["schema"], "manifest_schema") != 1:
        raise ParityFormatError("manifest_schema_version", repr(data["schema"]))
    base_commit = data["base_commit"]
    if type(base_commit) is not str:
        raise ParityFormatError("manifest_base_commit_not_str", repr(base_commit))
    functions_obj = _require_object(data["functions"], "manifest_functions")
    if set(functions_obj.keys()) != set(KNOWN_FUNCTIONS):
        raise ParityFormatError("manifest_functions_keys", repr(sorted(functions_obj.keys())))

    functions: dict[str, Any] = {}
    for name in KNOWN_FUNCTIONS:
        cfg = _require_object(functions_obj[name], f"manifest_function_{name}")
        _require_keys(cfg, FUNCTION_KEYS, f"manifest_function_{name}")
        frozen_path = cfg["frozen"]
        if type(frozen_path) is not str:
            raise ParityFormatError(f"manifest_function_{name}_frozen_not_str", repr(frozen_path))
        resolve(frozen_path, f"manifest_function_{name}_frozen")
        source_sha256 = cfg["source_sha256"]
        if type(source_sha256) is not str or _SOURCE_SHA256_RE.fullmatch(source_sha256) is None:
            raise ParityFormatError(
                f"manifest_function_{name}_source_sha256_invalid", repr(source_sha256)
            )
        subject = cfg["subject"]
        if subject is not None:
            if type(subject) is not str:
                raise ParityFormatError(f"manifest_function_{name}_subject_not_str", repr(subject))
            resolve(subject, f"manifest_function_{name}_subject")
        wired = _require_bool(cfg["wired"], f"manifest_function_{name}_wired")
        functions[name] = {
            "frozen": frozen_path,
            "source_sha256": source_sha256,
            "subject": subject,
            "wired": wired,
        }
    return {"schema": 1, "base_commit": base_commit, "functions": functions}


def load_frozen(path: Path, *, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _require_object(_read_json(path, "frozen"), "frozen")
    _require_keys(data, FROZEN_KEYS, "frozen")
    if _require_int(data["schema"], "frozen_schema") != 1:
        raise ParityFormatError("frozen_schema_version", repr(data["schema"]))
    function = data["function"]
    if function not in KNOWN_FUNCTIONS:
        raise ParityFormatError("frozen_function_unknown", repr(function))
    if manifest is not None and function not in manifest["functions"]:
        raise ParityFormatError("frozen_function_not_in_manifest", repr(function))
    count = _require_int(data["count"], "frozen_count")
    rows_raw = _require_list(data["rows"], "frozen_rows")
    if count != len(rows_raw) or len(rows_raw) == 0:
        raise ParityFormatError("frozen_count_mismatch", f"count={count} rows={len(rows_raw)}")

    rows: list[dict[str, Any]] = []
    inputs: list[str | None] = []
    for row_raw in rows_raw:
        row = _require_object(row_raw, "frozen_row")
        _require_keys(row, FROZEN_ROW_KEYS, "frozen_row")
        value_input = _require_str_or_none(row["input"], "frozen_row_input")
        old = _require_str_or_none(row["old"], "frozen_row_old")
        if old is not None and not value_ok_for_function(function, old):
            raise ParityFormatError("frozen_row_old_not_canonical", repr(old))
        inputs.append(value_input)
        rows.append({"input": value_input, "old": old})
    _check_ordered_inputs(inputs, "frozen_rows")

    return {"schema": 1, "function": function, "count": count, "rows": rows}


def load_exceptions(
    path: Path, *, manifest: dict[str, Any], frozen: dict[str, Any]
) -> dict[str, Any]:
    data = _require_object(_read_json(path, "exceptions"), "exceptions")
    _require_keys(data, EXCEPTIONS_KEYS, "exceptions")
    if _require_int(data["schema"], "exceptions_schema") != 1:
        raise ParityFormatError("exceptions_schema_version", repr(data["schema"]))
    function = data["function"]
    if function != frozen["function"]:
        raise ParityFormatError("exceptions_function_mismatch", repr(function))
    fn_cfg = manifest["functions"].get(function)
    if fn_cfg is None:
        raise ParityFormatError("exceptions_function_not_in_manifest", repr(function))
    subject = data["subject"]
    if subject != fn_cfg["subject"]:
        raise ParityFormatError("exceptions_subject_mismatch", repr(subject))
    if subject is None:
        raise ParityFormatError("exceptions_no_subject", repr(function))
    count = _require_int(data["count"], "exceptions_count")
    rows_raw = _require_list(data["rows"], "exceptions_rows")
    if count != len(rows_raw) or len(rows_raw) == 0:
        raise ParityFormatError("exceptions_count_mismatch", f"count={count} rows={len(rows_raw)}")

    frozen_old = {row["input"]: row["old"] for row in frozen["rows"]}
    inputs: list[str | None] = []
    rows: list[dict[str, Any]] = []
    for row_raw in rows_raw:
        row = _require_object(row_raw, "exceptions_row")
        _require_keys(row, EXCEPTIONS_ROW_KEYS, "exceptions_row")
        value_input = _require_str_or_none(row["input"], "exceptions_row_input")
        if value_input not in frozen_old:
            raise ParityFormatError("exceptions_row_input_not_in_corpus", repr(value_input))
        old = _require_str_or_none(row["old"], "exceptions_row_old")
        new = _require_str_or_none(row["new"], "exceptions_row_new")
        if old is not None and not value_ok_for_function(function, old):
            raise ParityFormatError("exceptions_row_old_not_canonical", repr(old))
        if new is not None and not value_ok_for_function(function, new):
            raise ParityFormatError("exceptions_row_new_not_canonical", repr(new))
        if old != frozen_old[value_input]:
            raise ParityFormatError("exceptions_row_old_diverges_from_frozen", repr(value_input))
        if old == new:
            raise ParityFormatError("exceptions_row_old_equals_new", repr(value_input))
        if old is not None and new is None:
            kind_expected = "loss"
        elif old is None and new is not None:
            kind_expected = "gain"
        else:
            kind_expected = "value"
        kind = row["kind"]
        if kind != kind_expected:
            raise ParityFormatError("exceptions_row_kind_incoherent", repr((value_input, kind)))
        cls = row["class"]
        allowed = ALL_CLASSES.get(kind, frozenset())
        if type(cls) is not str or cls not in allowed:
            raise ParityFormatError("exceptions_row_class_invalid", repr((kind, cls)))
        note = row["note"]
        if type(note) is not str:
            raise ParityFormatError("exceptions_row_note_not_str", repr(note))
        if cls.endswith("_other") and note == "":
            raise ParityFormatError("exceptions_row_note_required", repr(value_input))
        inputs.append(value_input)
        rows.append(
            {"input": value_input, "old": old, "new": new, "kind": kind, "class": cls, "note": note}
        )
    _check_ordered_inputs(inputs, "exceptions_rows")

    return {"schema": 1, "function": function, "subject": subject, "count": count, "rows": rows}


def _escape_invisible(text: str) -> str:
    out = []
    for c in text:
        if not c.isascii() and unicodedata.category(c) in _HIDDEN_CATEGORIES:
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return "".join(out)


def write_json(path: Path, obj: Any) -> None:
    text = _escape_invisible(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False)) + "\n"
    path.write_text(text, encoding="utf-8")


# --- Section 3.6: CLI ---

DEFAULT_REQUESTED = 600


def _corpus_path(root: Path) -> Path:
    return root / "tests" / "parity" / "corpus.json"


def _manifest_path(root: Path) -> Path:
    return root / "tests" / "parity" / "manifest.json"


def _frozen_path(root: Path, fn: str) -> Path:
    return root / "tests" / "parity" / f"{fn}.frozen.json"


def _exceptions_path(root: Path, fn: str) -> Path:
    return root / "tests" / "parity" / f"{fn}.exceptions.json"


def _compute_curated(harvested: Iterable[str | None]) -> list[str]:
    harvested_set = set(harvested)
    union: set[str | None] = set(TEST_LITERALS) | set(GRAMMAR_ROWS) | set(HOSTILE) | set(URL_SHAPED)
    remaining = (x for x in union - harvested_set if x is not None)
    return sorted(remaining, key=sort_key)


def _compute_generated(
    requested: int, harvested: Iterable[str | None], curated: Iterable[str]
) -> list[str]:
    both = set(harvested) | set(curated)
    dedup = _dedup_preserve_order(generate(requested))
    return sorted((x for x in dedup if x not in both), key=sort_key)


def _sha16(items: Any) -> str:
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()[:16]


def _call_subject(subject_path: str, s: str) -> Any:
    subject_callable = resolve(subject_path, "subject")
    if subject_path == "price_tracker.core.pricegrammar:parse_price_text":
        from price_tracker.core.pricegrammar import PriceContext

        return subject_callable(s, PriceContext())
    return subject_callable(s)


def cmd_corpus(args: argparse.Namespace) -> int:
    root: Path = args.root
    path = _corpus_path(root)

    if args.write:
        if path.exists() and not args.force:
            print(f"corpus: {path} already exists (use --force)", file=sys.stderr)
            return 1
        if args.harvested is None:
            print("corpus --write requires --harvested <json>", file=sys.stderr)
            return 1
        harvest_data = json.loads(Path(args.harvested).read_text(encoding="utf-8"))
        harvested = list(harvest_data["inputs"])
        curated = _compute_curated(harvested)
        generated = _compute_generated(DEFAULT_REQUESTED, harvested, curated)
        corpus = {
            "schema": 1,
            "count": len(harvested) + len(curated) + len(generated),
            "harvested": harvested,
            "curated": curated,
            "generated": {"seed": SEED, "requested": DEFAULT_REQUESTED, "inputs": generated},
        }
        write_json(path, corpus)
        full = sorted(set(harvested) | set(curated) | set(generated), key=sort_key)
        print(
            f"corpus: {len(full)} inputs (harvested {len(harvested)}, curated {len(curated)}, "
            f"generated {len(generated)}) sha256 {_sha16(full)}"
        )
        return 0

    data = load_corpus(path)
    harvested = data["harvested"]
    curated_expected = _compute_curated(harvested)
    requested = data["generated"]["requested"]
    generated_expected = _compute_generated(requested, harvested, curated_expected)
    ok = data["curated"] == curated_expected and data["generated"]["inputs"] == generated_expected
    full = sorted(
        set(harvested) | set(data["curated"]) | set(data["generated"]["inputs"]), key=sort_key
    )
    print(
        f"corpus: {len(full)} inputs (harvested {len(harvested)}, curated {len(data['curated'])}, "
        f"generated {len(data['generated']['inputs'])}) sha256 {_sha16(full)}"
    )
    if not ok:
        if data["curated"] != curated_expected:
            print(
                "corpus: curated section is not the closed union of the four lists", file=sys.stderr
            )
        if data["generated"]["inputs"] != generated_expected:
            print(
                "corpus: generated section is not reproducible from generate(requested)",
                file=sys.stderr,
            )
        return 1
    return 0


class _HarvestPlugin:
    """Wraps every re-exported ``parse_price``/``detect_currency`` binding once."""

    def __init__(self) -> None:
        self.bindings = 0
        self.recorded: dict[str, list[str | None]] = {"parse_price": [], "detect_currency": []}

    def pytest_sessionstart(self, session: Any) -> None:
        from price_tracker.core import scraper_base as sb

        originals: dict[str, Callable[..., Any]] = {
            "parse_price": sb.parse_price,
            "detect_currency": sb.detect_currency,
        }

        pkg = importlib.import_module("price_tracker.scrapers")
        for modinfo in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            importlib.import_module(modinfo.name)

        recorded = self.recorded

        def make_wrapper(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
            def wrapper(*call_args: Any, **call_kwargs: Any) -> Any:
                if call_args:
                    recorded[name].append(call_args[0])
                return original(*call_args, **call_kwargs)

            return wrapper

        wrappers = {name: make_wrapper(name, original) for name, original in originals.items()}

        for modname, module in list(sys.modules.items()):
            if module is None or not modname.startswith("price_tracker."):
                continue
            for name, original in originals.items():
                try:
                    current = getattr(module, name, None)
                except AttributeError:  # a broken descriptor must not abort the harvest
                    continue
                if current is original:
                    setattr(module, name, wrappers[name])
                    self.bindings += 1


def cmd_harvest(args: argparse.Namespace) -> int:
    import pytest

    root: Path = args.root
    tests_arg = Path(args.tests)
    tests_path = tests_arg if tests_arg.is_absolute() else root / tests_arg

    plugin = _HarvestPlugin()
    rc = pytest.main(
        [str(tests_path), "-q", "-o", "addopts=", "-p", "no:cacheprovider"], plugins=[plugin]
    )

    pp = sorted(set(plugin.recorded["parse_price"]), key=sort_key)
    dc = sorted(set(plugin.recorded["detect_currency"]), key=sort_key)
    inputs = sorted(set(pp) | set(dc), key=sort_key)
    out = {"bindings": plugin.bindings, "parse_price": pp, "detect_currency": dc, "inputs": inputs}
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    corpus_path = _corpus_path(root)
    if corpus_path.exists():
        corpus_data = load_corpus(corpus_path)
        corpus_all = (
            set(corpus_data["harvested"])
            | set(corpus_data["curated"])
            | set(corpus_data["generated"]["inputs"])
        )
        not_in_corpus = sum(1 for x in inputs if x not in corpus_all)
        print(f"harvest: {len(inputs)} inputs, {not_in_corpus} not in corpus")
    else:
        print(f"harvest: {len(inputs)} inputs")
    return 0 if rc == 0 else 1


def cmd_record(args: argparse.Namespace) -> int:
    root: Path = args.root
    fn: str = args.function
    manifest = load_manifest(_manifest_path(root))
    cfg = manifest["functions"][fn]

    if cfg["wired"]:
        print(
            f"record: {fn} is wired; frozen rows are never regenerated from the live code",
            file=sys.stderr,
        )
        return 1

    live_callable = resolve(cfg["frozen"], f"record_{fn}")
    live_source_sha = hashlib.sha256(inspect.getsource(live_callable).encode()).hexdigest()
    if live_source_sha != cfg["source_sha256"]:
        print(
            f"record: live source of {fn} no longer matches the pinned source_sha256",
            file=sys.stderr,
        )
        return 1

    out_path = _frozen_path(root, fn)
    if out_path.exists() and not args.force:
        print(f"record: {out_path} already exists (use --force)", file=sys.stderr)
        return 1

    corpus = load_corpus(_corpus_path(root))
    full_inputs = sorted(
        set(corpus["harvested"]) | set(corpus["curated"]) | set(corpus["generated"]["inputs"]),
        key=sort_key,
    )
    rows = [{"input": s, "old": serialize(live_callable(s))} for s in full_inputs]
    frozen = {"schema": 1, "function": fn, "count": len(rows), "rows": rows}
    write_json(out_path, frozen)
    print(f"record: wrote {len(rows)} rows to {out_path}")
    return 0


def cmd_exceptions(args: argparse.Namespace) -> int:
    root: Path = args.root
    fn: str = args.function
    manifest = load_manifest(_manifest_path(root))
    cfg = manifest["functions"][fn]
    subject = cfg["subject"]
    if subject is None:
        print(f"exceptions: {fn} has no subject", file=sys.stderr)
        return 1

    frozen = load_frozen(_frozen_path(root, fn), manifest=manifest)

    exc_path = _exceptions_path(root, fn)
    existing_notes: dict[str | None, str] = {}
    if exc_path.exists():
        existing = load_exceptions(exc_path, manifest=manifest, frozen=frozen)
        existing_notes = {row["input"]: row["note"] for row in existing["rows"]}

    rows = []
    for row in frozen["rows"]:
        s = row["input"]
        old = row["old"]
        new = None if s is None else serialize(_call_subject(subject, s))
        if new == old:
            continue
        kind, cls = classify(s, old, new)
        rows.append(
            {
                "input": s,
                "old": old,
                "new": new,
                "kind": kind,
                "class": cls,
                "note": existing_notes.get(s, ""),
            }
        )

    if not args.write:
        for row in rows:
            shown_input = json.dumps(row["input"], ensure_ascii=False)
            print(
                f"{row['kind']:<5} {row['class']:<24} {shown_input:<30} {row['old']}  {row['new']}"
            )
        print(f"exceptions: {len(rows)} rows (not written; pass --write)")
        return 0

    exceptions = {"schema": 1, "function": fn, "subject": subject, "count": len(rows), "rows": rows}
    write_json(exc_path, exceptions)
    print(f"exceptions: wrote {len(rows)} rows to {exc_path}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    root: Path = args.root
    fn: str = args.function
    manifest = load_manifest(_manifest_path(root))
    cfg = manifest["functions"][fn]
    subject_path = args.subject or cfg["subject"]
    if subject_path is None:
        print(f"diff: no subject for {fn} (pass --subject)", file=sys.stderr)
        return 1

    frozen = load_frozen(_frozen_path(root, fn), manifest=manifest)

    exc_path = _exceptions_path(root, fn)
    listed: dict[str | None, dict[str, Any]] = {}
    if exc_path.exists():
        existing = load_exceptions(exc_path, manifest=manifest, frozen=frozen)
        listed = {row["input"]: row for row in existing["rows"]}

    same = loss = value = gain = 0
    current: dict[str | None, tuple[str | None, str, str]] = {}
    print(f"{'kind':<5} {'class':<24} {'input':<30} old  new")
    for row in frozen["rows"]:
        s = row["input"]
        old = row["old"]
        new = None if s is None else serialize(_call_subject(subject_path, s))
        if new == old:
            same += 1
            continue
        kind, cls = classify(s, old, new)
        if kind == "loss":
            loss += 1
        elif kind == "value":
            value += 1
        else:
            gain += 1
        current[s] = (new, kind, cls)
        print(f"{kind:<5} {cls:<24} {json.dumps(s, ensure_ascii=False):<30} {old}  {new}")

    print(f"same={same} loss={loss} value={value} gain={gain}")

    if listed:
        unlisted = sum(1 for s in current if s not in listed)
        stale = sum(
            1 for s, row in listed.items() if s not in current or current[s][0] != row["new"]
        )
        print(f"listed={len(listed)} unlisted={unlisted} stale={stale}")
        if unlisted + stale > 0:
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    corpus_parser = subparsers.add_parser("corpus")
    corpus_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    corpus_group = corpus_parser.add_mutually_exclusive_group(required=True)
    corpus_group.add_argument("--check", action="store_true")
    corpus_group.add_argument("--write", action="store_true")
    corpus_parser.add_argument("--harvested", type=str, default=None)
    corpus_parser.add_argument("--force", action="store_true")
    corpus_parser.set_defaults(func=cmd_corpus)

    harvest_parser = subparsers.add_parser("harvest")
    harvest_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    harvest_parser.add_argument("--out", type=str, required=True)
    harvest_parser.add_argument("--tests", type=str, default="tests/unit")
    harvest_parser.set_defaults(func=cmd_harvest)

    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    record_parser.add_argument("function", choices=KNOWN_FUNCTIONS)
    record_parser.add_argument("--force", action="store_true")
    record_parser.set_defaults(func=cmd_record)

    exceptions_parser = subparsers.add_parser("exceptions")
    exceptions_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    exceptions_parser.add_argument("function", choices=KNOWN_FUNCTIONS)
    exceptions_parser.add_argument("--write", action="store_true")
    exceptions_parser.set_defaults(func=cmd_exceptions)

    diff_parser = subparsers.add_parser("diff")
    diff_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    diff_parser.add_argument("function", choices=KNOWN_FUNCTIONS)
    diff_parser.add_argument("--subject", type=str, default=None)
    diff_parser.set_defaults(func=cmd_diff)

    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
    except ParityFormatError as exc:
        print(f"{args.command}: format error: {exc}", file=sys.stderr)
        return 2
    return result


if __name__ == "__main__":
    sys.exit(main())
