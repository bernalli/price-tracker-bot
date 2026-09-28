"""Currency symbol table: the curated prototype table fused with a generated one.

The curated table is copied character-for-character from the prototype
(``pricegrammar.py``, 31 keys): it is precision-only evidence, one symbol maps to
every currency that writes it. ``generate_symbols`` derives a second table from
Babel's own per-territory, per-locale currency symbols, admitting only the keys
that cannot be mistaken for a word or an invisible character (D3): at least one
Unicode currency-sign (``Sc``) character, none of ``Cf``/``Zs``/``Zl``/``Zp``/``Cc``,
and never a key the curated table already owns. The generated data itself is
versioned separately, in ``_generated_currency_symbols.py``: ``generate_symbols``
recomputes it from the Babel installed on this machine and is never called at
import time, so a live Babel upgrade cannot silently change what the grammar
reads — only ``scripts/gen_currency_symbols.py --write`` can.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from babel.core import Locale, get_global
from babel.localedata import locale_identifiers
from babel.numbers import get_currency_symbol, get_territory_currencies

from price_tracker.core._generated_currency_symbols import GENERATED_SYMBOLS
from price_tracker.core.money import ACCEPTED_CURRENCIES

# Copied character-for-character from pricegrammar.py (83a91f6, lines 86-118) before that
# module re-exports this table instead of defining it.
CURATED_SYMBOLS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
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
)

_HIDDEN_CATEGORIES: Final = frozenset({"Cf", "Zs", "Zl", "Zp", "Cc"})


def _admitted(key: str) -> bool:
    if key in CURATED_SYMBOLS:
        return False
    categories = {unicodedata.category(c) for c in key}
    if "Sc" not in categories:
        return False
    return not categories & _HIDDEN_CATEGORIES


def generate_symbols() -> dict[str, frozenset[str]]:
    """Recompute the generated symbol table from the Babel installed here.

    Never called at import time: the live table always comes from the versioned
    ``_generated_currency_symbols`` module. Run ``scripts/gen_currency_symbols.py
    --write`` to refresh it after a Babel upgrade, then review the diff.
    """
    raw: dict[str, set[str]] = {}
    for territory in get_global("territory_currencies"):
        for code in get_territory_currencies(territory):
            if code not in ACCEPTED_CURRENCIES:
                continue
            for locale_id in locale_identifiers():
                try:
                    locale = Locale.parse(locale_id)
                except ValueError:
                    continue
                if locale.territory != territory:
                    continue
                symbol = get_currency_symbol(code, locale)
                if symbol != code:
                    raw.setdefault(symbol, set()).add(code)
    return {key: frozenset(codes) for key, codes in raw.items() if _admitted(key)}


def _validate_generated(table: object) -> Mapping[str, frozenset[str]]:
    """Defend the merge against a hand-edited or Babel-drifted generated file."""
    if not isinstance(table, Mapping):
        raise RuntimeError("_generated_currency_symbols.GENERATED_SYMBOLS is not a mapping")
    for key, value in table.items():
        if not isinstance(key, str) or not key:
            raise RuntimeError(f"generated currency symbol key {key!r} is not a non-empty str")
        if key in CURATED_SYMBOLS:
            raise RuntimeError(f"generated currency symbol key {key!r} shadows a curated key")
        if not _admitted(key):
            raise RuntimeError(f"generated currency symbol key {key!r} fails the admission rule")
        if not isinstance(value, frozenset) or not value:
            raise RuntimeError(f"generated currency symbol key {key!r} has an empty/bad value")
        if not value <= ACCEPTED_CURRENCIES:
            raise RuntimeError(f"generated currency symbol key {key!r} names a non-accepted code")
    return table


SYMBOLS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {**_validate_generated(GENERATED_SYMBOLS), **CURATED_SYMBOLS}
)
