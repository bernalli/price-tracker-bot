"""Monetary value object and the currency facts the price core relies on.

``Money`` is the only way a price leaves the price core: an amount that is a finite,
positive :class:`~decimal.Decimal` within the engine's bounds, and a currency that is
either an accepted ISO 4217 code or ``None`` (the page carried no currency; the
persistence boundary decides what happens next). A ``Money`` that violates these rules
cannot be constructed.

The accepted currency set is derived from Babel: every code tendered today by at least
one CLDR territory, minus the non-tender ``X*`` codes except the four regional tender
codes. The full currency engine (symbol table, expectations, cross-check) is a later
change; this module only fixes what the grammar and the resolver need.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from babel.core import get_global
from babel.numbers import get_currency_precision, get_territory_currencies

MAX_AMOUNT: Final = Decimal(10) ** 9
UNKNOWN_CURRENCY_MAX_FRACTION: Final = 3
_X_TENDER_EXCEPTIONS: Final = frozenset({"XAF", "XOF", "XPF", "XCD"})


def _tendered_currencies() -> frozenset[str]:
    """Return the ISO codes tendered today by some territory, minus non-tender X codes."""
    codes: set[str] = set()
    for territory in get_global("territory_currencies"):
        codes.update(get_territory_currencies(territory))
    return frozenset(c for c in codes if not c.startswith("X") or c in _X_TENDER_EXCEPTIONS)


ACCEPTED_CURRENCIES: Final[frozenset[str]] = _tendered_currencies()


def currency_precision(code: str) -> int:
    """Number of minor-unit digits of an accepted currency (JPY 0, EUR 2, BHD 3)."""
    if code not in ACCEPTED_CURRENCIES:
        raise ValueError(f"currency {code!r} is not accepted")
    return int(get_currency_precision(code))


def normalize_currency_code(value: object) -> str | None:
    """Map an untrusted currency field to an accepted ISO code, or ``None``.

    Only a string that, after trimming and upper-casing, is exactly one accepted code
    is recognised. Symbols, lists, numbers and unknown codes yield ``None``.
    """
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        return None
    return code if code in ACCEPTED_CURRENCIES else None


def significant_fraction_digits(amount: Decimal) -> int:
    """Count fraction digits of ``amount`` after dropping trailing zeros."""
    exponent = amount.normalize().as_tuple().exponent
    if not isinstance(exponent, int):  # NaN / infinity: callers reject these first
        raise ValueError("non-finite amount")
    return max(0, -exponent)


@dataclass(frozen=True, slots=True)
class Money:
    """A validated price: finite positive amount, accepted currency or ``None``."""

    amount: Decimal
    currency: str | None

    def __post_init__(self) -> None:
        if type(self.amount) is not Decimal:
            raise TypeError(f"amount must be a Decimal, got {type(self.amount).__name__}")
        if not self.amount.is_finite():
            raise ValueError("amount must be finite")
        if not (0 < self.amount <= MAX_AMOUNT):
            raise ValueError(f"amount {self.amount} outside (0, {MAX_AMOUNT}]")
        if self.currency is None:
            limit = UNKNOWN_CURRENCY_MAX_FRACTION
        else:
            if type(self.currency) is not str:
                raise TypeError("currency must be a str or None")
            if self.currency not in ACCEPTED_CURRENCIES:
                raise ValueError(f"currency {self.currency!r} is not accepted")
            limit = currency_precision(self.currency)
        if significant_fraction_digits(self.amount) > limit:
            raise ValueError(f"amount {self.amount} exceeds the precision of {self.currency}")
