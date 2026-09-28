"""Product view model: pure Python, no Telegram and no HTML.

There is no ``cross_store`` or ``scope_override`` field yet: per-product scope
arrives with its own migration. ``default_interval_minutes`` is carried so the
card can show the interval that actually applies, a per-product override or
the global default. ``reference_estimate`` arrives already converted by the
pricing service that computes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, get_args

from babel.numbers import list_currencies

from price_tracker.i18n.format import _check_amount

if TYPE_CHECKING:
    from decimal import Decimal

ProductStatus = Literal["active", "paused", "suspended"]
ThresholdType = Literal["percentage", "absolute", "target", "any_drop"]

_CURRENCY_CODE_RE: Final = re.compile(r"[A-Z]{3}")


def _check_currency(name: str, value: object) -> str:
    if not isinstance(value, str) or _CURRENCY_CODE_RE.fullmatch(value) is None:
        raise ValueError(f"{name}: must be a 3-letter uppercase code, got {value!r}")
    if value not in list_currencies():
        raise ValueError(f"{name}: unknown currency code {value!r}")
    return value


def _check_str(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name}: must be a str, got {value!r}")
    return value


def _check_int(name: str, value: object, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name}: must be an int, got {value!r}")
    if value < minimum:
        raise ValueError(f"{name}: must be >= {minimum}, got {value!r}")
    return value


def _check_optional_int(name: str, value: object, *, minimum: int) -> int | None:
    if value is None:
        return None
    return _check_int(name, value, minimum=minimum)


def _check_literal(name: str, value: object, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValueError(f"{name}: must be one of {allowed}, got {value!r}")


def _check_aware_datetime(name: str, value: object) -> None:
    if value is None:
        return
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name}: must be None or an aware datetime, got {value!r}")


@dataclass(frozen=True, slots=True)
class ProductView:
    """Everything a product card needs; prices stay in the product currency."""

    id: int
    name: str
    url: str
    domain: str
    currency: str
    current: Decimal | None
    initial: Decimal | None
    lowest: Decimal | None
    target: Decimal | None
    threshold_type: ThresholdType
    threshold_value: Decimal
    status: ProductStatus
    consecutive_errors: int
    check_interval_minutes: int | None
    default_interval_minutes: int
    last_checked_at: datetime | None
    reference_estimate: Decimal | None
    reference_currency: str

    def __post_init__(self) -> None:
        _check_int("id", self.id, minimum=1)
        _check_str("name", self.name)
        _check_str("url", self.url)
        _check_str("domain", self.domain)
        _check_currency("currency", self.currency)
        _check_currency("reference_currency", self.reference_currency)
        _check_amount("current", self.current, allow_none=True)
        _check_amount("initial", self.initial, allow_none=True)
        _check_amount("lowest", self.lowest, allow_none=True)
        _check_amount("target", self.target, allow_none=True)
        _check_amount("reference_estimate", self.reference_estimate, allow_none=True)
        _check_amount("threshold_value", self.threshold_value)
        _check_literal("threshold_type", self.threshold_type, get_args(ThresholdType))
        _check_literal("status", self.status, get_args(ProductStatus))
        _check_int("consecutive_errors", self.consecutive_errors, minimum=0)
        _check_optional_int("check_interval_minutes", self.check_interval_minutes, minimum=1)
        _check_int("default_interval_minutes", self.default_interval_minutes, minimum=1)
        _check_aware_datetime("last_checked_at", self.last_checked_at)
