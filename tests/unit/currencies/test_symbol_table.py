"""The currency symbol table: curated prototype, generated table, fusion.

The curated table is copied character-for-character from the prototype
(``pricegrammar.py``); the generated table is Babel output filtered by the
admission rule (D3); the fusion never lets a generated key shadow a curated
one.
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core import currency_symbols, pricegrammar
from price_tracker.core.currencies import ISO_CURRENCIES
from price_tracker.core.currency_symbols import CURATED_SYMBOLS, SYMBOLS, generate_symbols

REPO_ROOT = Path(__file__).resolve().parents[3]
GEN_SCRIPT = REPO_ROOT / "scripts" / "gen_currency_symbols.py"
GENERATED_MODULE = REPO_ROOT / "src" / "price_tracker" / "core" / "_generated_currency_symbols.py"

# Copied character-for-character from 83a91f6:src/price_tracker/core/pricegrammar.py:86-118.
PROTOTYPE_SYMBOLS: dict[str, frozenset[str]] = {
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
    "￥": frozenset({"JPY", "CNY"}),
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

# Raw Babel output (babel 2.18.0) containing RIGHT-TO-LEFT MARK (U+200F), before the
# admission rule runs. Pinned here, not derived from generate_symbols(), because that
# function already applies the admission rule and never exposes the raw candidates.
_RLM_RAW_KEYS = (
    "ج.م.‏",
    "د.أ.‏",
    "د.إ.‏",
    "د.ب.‏",
    "د.ت.‏",
    "د.ج.‏",
    "د.ع.‏",
    "د.ك.‏",
    "د.ل.‏",
    "د.م.‏",
    "ر.س.‏",
    "ر.ع.‏",
    "ر.ق.‏",
    "ر.ي.‏",
    "ل.س.‏",
    "ل.ل.‏",
)


def test_curated_symbols_equal_the_prototype() -> None:
    assert dict(CURATED_SYMBOLS) == PROTOTYPE_SYMBOLS
    for key, value in PROTOTYPE_SYMBOLS.items():
        assert SYMBOLS[key] == value


def test_generated_table_is_reproducible() -> None:
    from price_tracker.core._generated_currency_symbols import GENERATED_SYMBOLS

    assert generate_symbols() == dict(GENERATED_SYMBOLS), (
        "Babel changed: run scripts/gen_currency_symbols.py --write and review the diff"
    )


def test_admission_rule() -> None:
    from price_tracker.core._generated_currency_symbols import GENERATED_SYMBOLS

    assert len(GENERATED_SYMBOLS) == 23  # pinned to babel 2.18.0
    for key, value in GENERATED_SYMBOLS.items():
        categories = {unicodedata.category(c) for c in key}
        assert "Sc" in categories, f"{key!r} has no currency-sign character"
        assert not categories & {"Cf", "Zs", "Zl", "Zp", "Cc"}, f"{key!r} has a hidden character"
        assert key not in CURATED_SYMBOLS, f"{key!r} shadows a curated key"
        assert isinstance(value, frozenset), f"{key!r} value is not a frozenset"
        assert value, f"{key!r} has an empty value"
        assert value <= ISO_CURRENCIES, f"{key!r} names a non-accepted currency"


def test_dangerous_symbols_never_admitted() -> None:
    dangerous = {
        "​",  # ZERO WIDTH SPACE (raw key for CVE)
        "R",
        "L",
        "K",
        "P",
        "S",
        "FCFA",
        "F CFA",  # NARROW NO-BREAK SPACE between F and CFA: the real Babel key
        *_RLM_RAW_KEYS,
    }
    for key in dangerous:
        assert key not in SYMBOLS, f"{key!r} must never be admitted"
    # Negative control, not a raw key: ordinary U+0020 space between F and CFA.
    # Different bytes from "F CFA" above — not to be confused for a transcription
    # error. Babel never generates this variant; it must still be absent from SYMBOLS.
    assert "F CFA" not in SYMBOLS


def test_new_symbols_extend_conservatively() -> None:
    curated_only = MappingProxyType(dict(CURATED_SYMBOLS))
    alphabet = sorted({*"0123456789.,'’    "} | {c for key in SYMBOLS for c in key})

    @given(text=st.text(alphabet=alphabet, max_size=20))
    @settings(max_examples=300, deadline=None)
    def check(text: str) -> None:
        with (
            mock.patch.object(pricegrammar, "SYMBOLS", curated_only),
            mock.patch.object(
                pricegrammar,
                "_SYMBOLS_LONGEST_FIRST",
                tuple(sorted(curated_only, key=len, reverse=True)),
            ),
        ):
            old = pricegrammar.parse_price_text(text)
        new = pricegrammar.parse_price_text(text)
        assert old is None or old == new

    check()


def test_pricegrammar_reads_the_merged_table() -> None:
    assert pricegrammar.SYMBOLS is currency_symbols.SYMBOLS
    ordered = pricegrammar._SYMBOLS_LONGEST_FIRST
    assert set(ordered) == set(pricegrammar.SYMBOLS)
    assert list(ordered) == sorted(ordered, key=len, reverse=True)


def test_iso_currencies_is_the_money_set() -> None:
    from price_tracker.core import money

    assert ISO_CURRENCIES is money.ACCEPTED_CURRENCIES
    for code in ("USD", "EUR", "XAF", "XOF", "XPF", "XCD"):
        assert code in ISO_CURRENCIES
    for code in ("XXX", "XTS", "XAU", "ADP", "XCG"):
        assert code not in ISO_CURRENCIES
    assert len(ISO_CURRENCIES) == 153, f"P2: the set is date-dependent (got {len(ISO_CURRENCIES)})"


def test_gen_script_check_mode(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    missing_key_copy = tmp_path / "missing_key.py"
    generated_text = GENERATED_MODULE.read_text(encoding="utf-8")
    one_key = next(iter(dict(generate_symbols())))
    mutated = generated_text.replace(f'"{one_key}": ', '"__removed__": ', 1)
    assert mutated != generated_text
    missing_key_copy.write_text(mutated, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check", str(missing_key_copy)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1

    extra_key_copy = tmp_path / "extra_key.py"
    extra_text = generated_text.replace(
        "GENERATED_SYMBOLS: dict[str, frozenset[str]] = {",
        'GENERATED_SYMBOLS: dict[str, frozenset[str]] = {\n    "$": frozenset({"USD"}),',
        1,
    )
    assert extra_text != generated_text
    extra_key_copy.write_text(extra_text, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check", str(extra_key_copy)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1

    result = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--write", str(extra_key_copy)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check", str(extra_key_copy)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
