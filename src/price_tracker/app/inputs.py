"""The input grammar every guided flow and command uses.

Every ``parse_*`` function is pure: it maps a ``str`` to a value or to an
:class:`InputError` carrying a known :class:`InputErrorCode`, and it never raises
on user data. A rejected input never reaches a service.

Common rules for numeric and enum inputs:

* surrounding whitespace is stripped; the stripped text must be non-empty and at
  most :data:`SCALAR_MAX_CHARS` characters;
* embedded control, format (bidi) and line/paragraph separator characters are
  rejected;
* amounts match ``[0-9]{1,9}(?:[.,][0-9]{1,4})?`` (ASCII digits only) before the
  ``Decimal`` conversion, with a comma replaced by a dot. A single dot or comma
  always denotes the decimal separator: ``1.299`` and ``1,299`` both mean 1.299,
  never 1299. Repeated or mixed separators, exponents, signs, ``NaN`` and
  ``Infinity`` are rejected by the grammar, so ``Decimal`` never sees raw text;
* integers match ``[0-9]{1,9}``, except user ids, which match ``[0-9]{1,19}``
  followed by the explicit ``1..9223372036854775807`` bound.

Nicknames keep raw text (HTML escaping happens only in the renderer) and URLs use
the shared URL grammar and the SSRF guard, without the scalar length limit.
"""

from __future__ import annotations

import re
import unicodedata
import zoneinfo
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING, Final

from babel.numbers import list_currencies

from price_tracker.core.url_utils import UnsafeURLError, validate_public_url

if TYPE_CHECKING:
    from collections.abc import Callable

SCALAR_MAX_CHARS: Final = 64
NICKNAME_MAX_VISIBLE: Final = 64
URL_MAX_CHARS: Final = 2048
USER_ID_MAX: Final = 9_223_372_036_854_775_807
PERCENT_RANGE: Final = (1, 99)
PRODUCT_INTERVAL_RANGE: Final = (5, 10_080)
GLOBAL_INTERVAL_RANGE: Final = (5, 10_080)
DIGEST_INTERVAL_RANGE: Final = (5, 1_440)
MUTE_HOURS_RANGE: Final = (1, 8_760)

ANY_DROP_SENTINELS: Final = frozenset({"any", "ogni", "sempre", "all"})
CANCEL_SENTINELS: Final = frozenset({"-", "no", "skip", "salta", "annulla"})

_REJECTED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_INVISIBLE_CATEGORIES: Final = frozenset({"Mn", "Me"})

_AMOUNT_RE: Final = re.compile(r"[0-9]{1,9}(?:[.,][0-9]{1,4})?")
_INT_RE: Final = re.compile(r"[0-9]{1,9}")
_USER_ID_RE: Final = re.compile(r"[0-9]{1,19}")
_PERCENT_RE: Final = re.compile(r"([0-9]{1,9})%")
_CURRENCY_RE: Final = re.compile(r"[A-Za-z]{3}")
_QUIET_RE: Final = re.compile(r"([0-9]{2}):([0-9]{2})-([0-9]{2}):([0-9]{2})")
_URL_RE: Final = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class InputErrorCode(StrEnum):
    """Every reason a parser can reject an input."""

    EMPTY = "empty"
    TOO_LONG = "too_long"
    CONTROL_CHARACTER = "control_character"
    NOT_A_NUMBER = "not_a_number"
    OUT_OF_RANGE = "out_of_range"
    NOT_A_CURRENCY = "not_a_currency"
    NOT_A_TIMEZONE = "not_a_timezone"
    NOT_A_TIME_RANGE = "not_a_time_range"
    NOT_A_URL = "not_a_url"
    UNSAFE_URL = "unsafe_url"


@dataclass(frozen=True, slots=True)
class InputError:
    """A rejected input; the hint shown to the user is chosen from ``code``."""

    code: InputErrorCode


@dataclass(frozen=True, slots=True)
class Cancel:
    """The user typed a cancel sentinel: close the prompt, change nothing."""


@dataclass(frozen=True, slots=True)
class Percentage:
    """A percentage drop threshold in ``1..99``."""

    value: int


@dataclass(frozen=True, slots=True)
class Absolute:
    """An absolute drop threshold (> 0) in the product currency."""

    amount: Decimal


@dataclass(frozen=True, slots=True)
class AnyDrop:
    """Alert on any drop."""


@dataclass(frozen=True, slots=True)
class SetTarget:
    """A target price (> 0) in the product currency."""

    amount: Decimal


@dataclass(frozen=True, slots=True)
class ClearTarget:
    """``0`` clears the target."""


@dataclass(frozen=True, slots=True)
class IntervalMinutes:
    """A per-product check interval in minutes."""

    minutes: int


@dataclass(frozen=True, slots=True)
class ResetInterval:
    """``0`` resets the per-product interval to the global one."""


@dataclass(frozen=True, slots=True)
class Off:
    """The literal ``off`` for settings that can be switched off."""


@dataclass(frozen=True, slots=True)
class Forever:
    """The literal ``forever`` for a mute without an end."""


@dataclass(frozen=True, slots=True)
class QuietHours:
    """A quiet-hours window; ``start != end``."""

    start: time
    end: time


ThresholdInput = Percentage | Absolute | AnyDrop | Cancel
TargetInput = SetTarget | ClearTarget | Cancel
ProductIntervalInput = IntervalMinutes | ResetInterval


def _normalise_scalar(text: str) -> str | InputError:
    """Apply the common rules shared by numeric and enum inputs."""
    stripped = text.strip()
    if not stripped:
        return InputError(InputErrorCode.EMPTY)
    if len(stripped) > SCALAR_MAX_CHARS:
        return InputError(InputErrorCode.TOO_LONG)
    if any(unicodedata.category(char) in _REJECTED_CATEGORIES for char in stripped):
        return InputError(InputErrorCode.CONTROL_CHARACTER)
    return stripped


def _parse_amount(text: str) -> Decimal | None:
    """Return the amount for grammar-conforming text, else ``None``."""
    if _AMOUNT_RE.fullmatch(text) is None:
        return None
    return Decimal(text.replace(",", "."))


def _parse_bounded_int(text: str, bounds: tuple[int, int]) -> int | InputError:
    if _INT_RE.fullmatch(text) is None:
        return InputError(InputErrorCode.NOT_A_NUMBER)
    value = int(text)
    low, high = bounds
    if not low <= value <= high:
        return InputError(InputErrorCode.OUT_OF_RANGE)
    return value


def parse_threshold(text: str) -> ThresholdInput | InputError:
    """``<int>%`` in 1..99, a decimal > 0, an any-drop or a cancel sentinel."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    folded = normalised.lower()
    if folded in ANY_DROP_SENTINELS:
        return AnyDrop()
    if folded in CANCEL_SENTINELS:
        return Cancel()
    percent = _PERCENT_RE.fullmatch(normalised)
    if percent is not None:
        value = int(percent.group(1))
        low, high = PERCENT_RANGE
        if not low <= value <= high:
            return InputError(InputErrorCode.OUT_OF_RANGE)
        return Percentage(value)
    amount = _parse_amount(normalised)
    if amount is None:
        return InputError(InputErrorCode.NOT_A_NUMBER)
    if amount <= 0:
        return InputError(InputErrorCode.OUT_OF_RANGE)
    return Absolute(amount)


def parse_target(text: str) -> TargetInput | InputError:
    """A decimal > 0 sets the target, ``0`` clears it, a cancel sentinel cancels."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    if normalised.lower() in CANCEL_SENTINELS:
        return Cancel()
    amount = _parse_amount(normalised)
    if amount is None:
        return InputError(InputErrorCode.NOT_A_NUMBER)
    if amount == 0:
        return ClearTarget()
    return SetTarget(amount)


def parse_product_interval(text: str) -> ProductIntervalInput | InputError:
    """Integer minutes: ``0`` resets to the global interval, else 5..10080."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    if _INT_RE.fullmatch(normalised) is None:
        return InputError(InputErrorCode.NOT_A_NUMBER)
    minutes = int(normalised)
    if minutes == 0:
        return ResetInterval()
    low, high = PRODUCT_INTERVAL_RANGE
    if not low <= minutes <= high:
        return InputError(InputErrorCode.OUT_OF_RANGE)
    return IntervalMinutes(minutes)


def parse_global_interval(text: str) -> int | InputError:
    """Integer minutes in 5..10080."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    return _parse_bounded_int(normalised, GLOBAL_INTERVAL_RANGE)


def parse_digest_interval(text: str) -> int | InputError:
    """Integer minutes in 5..1440."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    return _parse_bounded_int(normalised, DIGEST_INTERVAL_RANGE)


def parse_throttle(text: str) -> int | Off | InputError:
    """An integer >= 1, or ``off``."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    if normalised.lower() == "off":
        return Off()
    return _parse_bounded_int(normalised, (1, 999_999_999))


def parse_mute_hours(text: str) -> int | Forever | InputError:
    """Integer hours in 1..8760, or ``forever``."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    if normalised.lower() == "forever":
        return Forever()
    return _parse_bounded_int(normalised, MUTE_HOURS_RANGE)


def parse_user_id(text: str) -> int | InputError:
    """A Telegram user id: ``[0-9]{1,19}`` bounded by 1..9223372036854775807."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    if _USER_ID_RE.fullmatch(normalised) is None:
        return InputError(InputErrorCode.NOT_A_NUMBER)
    value = int(normalised)
    if not 1 <= value <= USER_ID_MAX:
        return InputError(InputErrorCode.OUT_OF_RANGE)
    return value


@cache
def _currency_codes() -> frozenset[str]:
    return frozenset(list_currencies())


def parse_currency_code(text: str) -> str | InputError:
    """Three ASCII letters, upper-cased, known to Babel's ISO 4217 list."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    if _CURRENCY_RE.fullmatch(normalised) is None:
        return InputError(InputErrorCode.NOT_A_CURRENCY)
    code = normalised.upper()
    if code not in _currency_codes():
        return InputError(InputErrorCode.NOT_A_CURRENCY)
    return code


@cache
def _timezones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def parse_timezone(text: str) -> str | InputError:
    """A member of ``zoneinfo.available_timezones()``, matched exactly."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    if normalised not in _timezones():
        return InputError(InputErrorCode.NOT_A_TIMEZONE)
    return normalised


def parse_quiet_hours(text: str) -> QuietHours | Off | InputError:
    """``HH:MM-HH:MM`` with valid times and ``start != end``, or ``off``."""
    normalised = _normalise_scalar(text)
    if isinstance(normalised, InputError):
        return normalised
    if normalised.lower() == "off":
        return Off()
    match = _QUIET_RE.fullmatch(normalised)
    if match is None:
        return InputError(InputErrorCode.NOT_A_TIME_RANGE)
    start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
    if max(start_h, end_h) > 23 or max(start_m, end_m) > 59:
        return InputError(InputErrorCode.NOT_A_TIME_RANGE)
    start, end = time(start_h, start_m), time(end_h, end_m)
    if start == end:
        return InputError(InputErrorCode.NOT_A_TIME_RANGE)
    return QuietHours(start, end)


def parse_nickname(text: str) -> str | InputError:
    """Raw text with 1..64 visible characters and no control characters."""
    stripped = text.strip()
    if any(unicodedata.category(char) in _REJECTED_CATEGORIES for char in stripped):
        return InputError(InputErrorCode.CONTROL_CHARACTER)
    visible = sum(1 for char in stripped if unicodedata.category(char) not in _INVISIBLE_CATEGORIES)
    if visible == 0:
        return InputError(InputErrorCode.EMPTY)
    if visible > NICKNAME_MAX_VISIBLE:
        return InputError(InputErrorCode.TOO_LONG)
    return stripped


def parse_url(text: str, *, guard: Callable[[str], None] = validate_public_url) -> str | InputError:
    """An ``http(s)`` URL matching the shared grammar that passes the SSRF guard.

    ``guard`` is injectable so property tests can run without DNS; production
    code uses the default.
    """
    stripped = text.strip()
    if not stripped:
        return InputError(InputErrorCode.EMPTY)
    if len(stripped) > URL_MAX_CHARS:
        return InputError(InputErrorCode.TOO_LONG)
    if any(unicodedata.category(char) in _REJECTED_CATEGORIES for char in stripped):
        return InputError(InputErrorCode.CONTROL_CHARACTER)
    if _URL_RE.fullmatch(stripped) is None:
        return InputError(InputErrorCode.NOT_A_URL)
    try:
        guard(stripped)
    except UnsafeURLError:
        return InputError(InputErrorCode.UNSAFE_URL)
    except ValueError:
        return InputError(InputErrorCode.NOT_A_URL)
    return stripped
