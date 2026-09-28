"""Not-well-formed inputs: trusted arguments raise, untrusted input never does.

``expected``/``declared`` are built by trusted code and are validated at the
boundary; ``text``/``url`` come from the page and must never raise, however
malformed. The generated symbol table is data on disk: a hand-edited or
Babel-drifted file must fail loudly at import, in a subprocess so the failure
never contaminates the test process's already-imported modules (the same
technique as ``tests/unit/test_price_core_boundary.py:64-69``, never
``importlib.reload``).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from price_tracker.core.currencies import (
    RegionalMiss,
    detect_currency,
    expected_currencies,
    parse_regional_price,
)

BAD_FROZENSET_MEMBERS = [
    {"EUR"},  # a set, not a frozenset
    frozenset(),  # declared must be non-empty when given
    frozenset({"xts"}),  # not accepted (case aside)
    frozenset({"eur"}),  # lower-case
    frozenset({"EURO"}),  # not three letters
    frozenset({None}),  # not a string member
    ["EUR"],  # a list, not a frozenset
    "EUR",  # a bare string, not a frozenset of one
]


@pytest.mark.parametrize("bad", BAD_FROZENSET_MEMBERS, ids=repr)
def test_bad_expected_argument_raises(bad: object) -> None:
    if bad == frozenset():
        # An empty frozenset is a legitimate "no signal" expectation for detect_currency,
        # but declared must be non-empty when given at all: tested separately below.
        pytest.skip("empty frozenset is valid for expected; see test_empty_declared_raises")
    with pytest.raises(ValueError, match="bad_expected"):
        detect_currency("10", expected=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", BAD_FROZENSET_MEMBERS, ids=repr)
def test_bad_declared_argument_raises(bad: object) -> None:
    with pytest.raises(ValueError, match="bad_declared"):
        expected_currencies("https://shop.example.de/p", declared=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bad_declared"):
        parse_regional_price("10", url="https://shop.example.de/p", declared=bad)  # type: ignore[arg-type]


def test_empty_declared_raises() -> None:
    with pytest.raises(ValueError, match="bad_declared"):
        expected_currencies("https://shop.example.de/p", declared=frozenset())
    with pytest.raises(ValueError, match="bad_declared"):
        parse_regional_price("10", url="https://shop.example.de/p", declared=frozenset())


@pytest.mark.parametrize("digits", ["roman", "", "LATIN", 1])
def test_bad_digits_raises(digits: object) -> None:
    with pytest.raises(ValueError, match="digit script"):
        parse_regional_price("10", url="https://shop.example.de/p", digits=digits)  # type: ignore[arg-type]


@pytest.mark.parametrize("grouping", ["chinese", "", "AUTO", 1])
def test_bad_grouping_raises(grouping: object) -> None:
    with pytest.raises(ValueError, match="grouping"):
        parse_regional_price("10", url="https://shop.example.de/p", grouping=grouping)  # type: ignore[arg-type]


def test_regional_miss_validates_its_reason() -> None:
    with pytest.raises(ValueError, match="unknown regional-miss reason"):
        RegionalMiss("other")


@pytest.mark.parametrize(
    "text",
    [
        "€10\udcff",  # a lone surrogate
        "\x0010",
        b"\x0010",
        10,
        10.5,
        None,
    ],
)
def test_untrusted_text_never_raises(text: object) -> None:
    result = detect_currency(text)
    assert result is None or isinstance(result, str)
    outcome = parse_regional_price(text, url="https://shop.example.de/p")
    assert outcome is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.example.de/\udcff",
        "https://shop.example.de/\x00",
        b"https://shop.example.de/p",
        10,
        None,
    ],
)
def test_untrusted_url_never_raises(url: object) -> None:
    result = expected_currencies(url)
    assert result == frozenset()
    outcome = parse_regional_price("10", url=url)
    assert outcome is not None


# --- the generated-module import guard, exercised in a subprocess -----------------

_HARNESS = """
import sys, types

module = types.ModuleType("price_tracker.core._generated_currency_symbols")
module.GENERATED_SYMBOLS = {VARIANT}
sys.modules["price_tracker.core._generated_currency_symbols"] = module

try:
    from price_tracker.core import currency_symbols
except RuntimeError as exc:
    print("RUNTIME_ERROR", exc)
else:
    print("OK", len(currency_symbols.SYMBOLS))
"""

_POSITIVE_CONTROL = """
from price_tracker.core import currency_symbols
print("OK", len(currency_symbols.SYMBOLS))
"""

VARIANTS = {
    "value_names_a_non_accepted_currency": '{"\\u20a6": frozenset({"XTS"})}',
    "empty_value": '{"\\u20a6": frozenset()}',
    "value_is_a_set_not_a_frozenset": '{"\\u20a6": {"NGN"}}',
    "empty_key": '{"": frozenset({"NGN"})}',
    "key_is_zero_width_space": '{"\\u200b": frozenset({"CVE"})}',
    "key_shadows_a_curated_symbol": '{"$": frozenset({"USD"})}',
    "key_is_not_a_string": '{1: frozenset({"USD"})}',
    "value_is_a_list": '{"\\u20a6": ["NGN"]}',
    "value_is_lower_case": '{"\\u20a6": frozenset({"ngn"})}',
    "key_has_a_control_character": '{"$\\n": frozenset({"USD"})}',
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_malformed_generated_table_raises_runtime_error(name: str) -> None:
    script = _HARNESS.replace("{VARIANT}", VARIANTS[name])
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("RUNTIME_ERROR"), (name, result.stdout, result.stderr)
    assert "Traceback" not in result.stderr, result.stderr


def test_generated_table_not_a_mapping_raises_runtime_error() -> None:
    script = _HARNESS.replace("{VARIANT}", "[]")
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("RUNTIME_ERROR"), (result.stdout, result.stderr)
    assert "not a mapping" in result.stdout.lower()


def test_intact_module_imports_with_the_full_table() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _POSITIVE_CONTROL], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"OK {31 + 23}"
