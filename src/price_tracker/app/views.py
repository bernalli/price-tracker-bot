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
from zoneinfo import ZoneInfo

from babel.numbers import list_currencies

from price_tracker.i18n.format import _check_amount

if TYPE_CHECKING:
    from decimal import Decimal

ProductStatus = Literal["active", "paused", "suspended"]
ThresholdType = Literal["percentage", "absolute", "target", "any_drop"]
ListFilter = Literal["a", "p", "e"]

PAGE_SIZE: Final = 5

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


def _check_bool(name: str, value: object) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name}: must be a bool, got {value!r}")


def _check_optional_str(name: str, value: object) -> None:
    if value is not None:
        _check_str(name, value)


def _check_timezone(name: str, value: object) -> None:
    text = _check_str(name, value)
    try:
        ZoneInfo(text)
    except (KeyError, ValueError, OSError) as exc:
        raise ValueError(f"{name}: not a zoneinfo key, got {value!r}") from exc


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


@dataclass(frozen=True, slots=True)
class PrefsView:
    """A user's effective global notification preferences, as the settings panel shows them."""

    mute: bool
    mute_until: datetime | None
    digest_mode: bool
    digest_interval_minutes: int
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    throttle_per_hour: int | None
    timezone: str

    def __post_init__(self) -> None:
        _check_bool("mute", self.mute)
        _check_aware_datetime("mute_until", self.mute_until)
        _check_bool("digest_mode", self.digest_mode)
        _check_int("digest_interval_minutes", self.digest_interval_minutes, minimum=1)
        _check_optional_str("quiet_hours_start", self.quiet_hours_start)
        _check_optional_str("quiet_hours_end", self.quiet_hours_end)
        _check_optional_int("throttle_per_hour", self.throttle_per_hour, minimum=1)
        _check_timezone("timezone", self.timezone)


@dataclass(frozen=True, slots=True)
class ListPage:
    """One page of the product list: ``items`` are the ``total`` matches of ``filter``, sliced."""

    filter: ListFilter
    page: int
    pages: int
    total: int
    items: tuple[ProductView, ...]

    def __post_init__(self) -> None:
        _check_literal("filter", self.filter, get_args(ListFilter))
        _check_int("pages", self.pages, minimum=1)
        _check_int("page", self.page, minimum=1)
        if self.page > self.pages:
            raise ValueError(f"page: must be <= pages ({self.pages}), got {self.page!r}")
        _check_int("total", self.total, minimum=0)
        if len(self.items) > PAGE_SIZE:
            raise ValueError(f"items: at most {PAGE_SIZE}, got {len(self.items)}")


@dataclass(frozen=True, slots=True)
class HomeView:
    """What the Home screen shows: the user's product counts and whether they are an admin."""

    active: int
    paused: int
    is_admin: bool

    def __post_init__(self) -> None:
        _check_int("active", self.active, minimum=0)
        _check_int("paused", self.paused, minimum=0)
        _check_bool("is_admin", self.is_admin)
