"""Pure formatting helpers over Babel, with an explicit ``locale`` and no state.

Every function takes ``locale`` as a required keyword: this package never reads
a ``ContextVar`` (it is a leaf package; the runtime that owns the active
locale lives in ``price_tracker.bot.messages``). An unknown locale always
raises ``ValueError`` (never a Babel exception type), and every amount is
validated before it reaches a comparison or Babel, so a not-well-formed input
never surfaces as ``decimal.InvalidOperation`` or another Babel-internal
error.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, cast
from zoneinfo import ZoneInfo

from babel.dates import format_datetime, format_timedelta
from babel.numbers import format_currency, format_percent, get_currency_precision, list_currencies
from babel.units import format_unit

from price_tracker.i18n.locales import babel_locale

_CHANGE_DOWN: Final = "▼"
_CHANGE_UP: Final = "▲"

_CURRENCY_CODE_RE: Final = re.compile(r"[A-Z]{3}")
_AMOUNT_CEILING: Final = Decimal(10) ** 15


def _check_amount(name: str, value: object, *, allow_none: bool = False) -> Decimal | None:
    """Validate a price-shaped ``Decimal``; never lets a bad value reach Babel."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{name}: must not be None")
    if not isinstance(value, Decimal):
        raise ValueError(f"{name}: must be a Decimal, got {value!r}")
    if not value.is_finite():
        raise ValueError(f"{name}: must be finite, got {value!r}")
    if value.is_signed():
        raise ValueError(f"{name}: must not be negative, got {value!r}")
    if value >= _AMOUNT_CEILING:
        raise ValueError(f"{name}: must be below 10**15, got {value!r}")
    return value


def _require_amount(name: str, value: object) -> Decimal:
    """Like ``_check_amount`` with ``allow_none=False``, but typed as non-optional."""
    return cast("Decimal", _check_amount(name, value))


def _check_currency(currency: object) -> str:
    """Validate a 3-letter uppercase ISO 4217 code, known to Babel."""
    if not isinstance(currency, str) or _CURRENCY_CODE_RE.fullmatch(currency) is None:
        raise ValueError(f"currency: not a 3-letter uppercase code, got {currency!r}")
    if currency not in list_currencies():
        raise ValueError(f"currency: unknown code {currency!r}")
    return currency


def money(amount: Decimal, currency: str, *, locale: str) -> str:
    """Render an amount, quantized to the currency's precision with half-up rounding."""
    loc = babel_locale(locale)
    checked_currency = _check_currency(currency)
    checked_amount = _require_amount("amount", amount)
    digits = get_currency_precision(checked_currency)
    quantum = Decimal(1).scaleb(-digits)
    quantized = checked_amount.quantize(quantum, rounding=ROUND_HALF_UP)
    return format_currency(quantized, checked_currency, locale=loc, currency_digits=True)


def delta(old: Decimal, new: Decimal, currency: str, *, locale: str) -> str:
    """``▼``/``▲`` plus the absolute change, or ``""`` when ``old == new``."""
    babel_locale(locale)
    checked_old = _require_amount("old", old)
    checked_new = _require_amount("new", new)
    if checked_new < checked_old:
        return f"{_CHANGE_DOWN} {money(checked_old - checked_new, currency, locale=locale)}"
    if checked_new > checked_old:
        return f"{_CHANGE_UP} {money(checked_new - checked_old, currency, locale=locale)}"
    return ""


def percent(ratio: Decimal, *, locale: str) -> str:
    """A percentage rounded half-up to one decimal, never Babel's half-even default."""
    loc = babel_locale(locale)
    checked = _require_amount("ratio", ratio)
    scaled = (checked * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    quantized_ratio = scaled / 100
    pattern = copy.copy(loc.percent_formats[None])
    pattern.frac_prec = (0, 1)
    return format_percent(quantized_ratio, format=pattern, locale=loc)


def change(initial: Decimal, current: Decimal, *, locale: str) -> str:
    """``▼``/``▲`` plus the percentage change from ``initial``, or ``""``."""
    babel_locale(locale)
    checked_initial = _require_amount("initial", initial)
    checked_current = _require_amount("current", current)
    if checked_initial <= 0 or checked_current == checked_initial:
        return ""
    ratio = abs(checked_current - checked_initial) / checked_initial
    glyph = _CHANGE_DOWN if checked_current < checked_initial else _CHANGE_UP
    return f"{glyph} {percent(ratio, locale=locale)}"


def ago(delta: timedelta, *, locale: str) -> str | None:  # noqa: A002
    """A relative-past phrase, or ``None`` under one minute (the caller renders "just now")."""
    loc = babel_locale(locale)
    if delta < timedelta(minutes=1):
        return None
    return format_timedelta(-delta, locale=loc, add_direction=True, granularity="minute")


def duration(minutes: int, *, locale: str) -> str:
    """An exact interval, choosing the unit that divides ``minutes`` evenly."""
    loc = babel_locale(locale)
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise ValueError(f"minutes: must be an int, got {minutes!r}")
    if minutes < 1:
        raise ValueError(f"minutes: must be >= 1, got {minutes!r}")
    if minutes % 1440 == 0:
        unit, unit_minutes = "day", 1440
    elif minutes % 60 == 0:
        unit, unit_minutes = "hour", 60
    else:
        unit, unit_minutes = "minute", 1
    count = minutes // unit_minutes
    return format_unit(count, f"duration-{unit}", length="short", locale=loc)


def when(instant: datetime, *, tz: str, locale: str) -> str:
    """An absolute short date/time in ``tz``."""
    loc = babel_locale(locale)
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise ValueError(f"instant: must be an aware datetime, got {instant!r}")
    if not isinstance(tz, str):
        raise TypeError(f"tz: must be a str, got {tz!r}")
    try:
        zone = ZoneInfo(tz)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"tz: not a zoneinfo key, got {tz!r}") from exc
    return format_datetime(instant, format="short", tzinfo=zone, locale=loc)
