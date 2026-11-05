"""The smallest price drop worth an alert.

A drop of a few cents on an expensive product (199.49 -> 199.00) is noise, yet a
user tracking "any drop" was told about it. Every price alert therefore also has
to clear a floor: the drop must be at least ``percent`` of the old price AND at
least ``absolute`` in the product's currency. The floor comes on top of the
product's own rule (any drop, percentage, absolute, target), never instead of it.

The server default comes from ``MIN_DROP_PERCENT`` / ``MIN_DROP_ABSOLUTE``; a user
may pick another percentage preset, or switch the floor off entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

DEFAULT_MIN_DROP_PERCENT: Final = Decimal(2)
DEFAULT_MIN_DROP_ABSOLUTE: Final = Decimal(1)
# The stored per-user choices: "off", or the percentage of a preset.
MIN_DROP_CHOICES: Final = ("off", "1", "2", "5")


@dataclass(frozen=True, slots=True)
class DropFloor:
    """Both parts of the floor; a part set to zero does not constrain."""

    percent: Decimal
    absolute: Decimal

    def __post_init__(self) -> None:
        for name in ("percent", "absolute"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name}: must be a finite Decimal >= 0, got {value!r}")

    @property
    def is_off(self) -> bool:
        """Whether the floor lets every drop through."""
        return self.percent == 0 and self.absolute == 0


DEFAULT_DROP_FLOOR: Final = DropFloor(DEFAULT_MIN_DROP_PERCENT, DEFAULT_MIN_DROP_ABSOLUTE)
NO_DROP_FLOOR: Final = DropFloor(Decimal(0), Decimal(0))


def parse_floor_value(raw: str | None, *, name: str, default: Decimal) -> Decimal:
    """Parse one part of the floor from the environment: a finite number >= 0."""
    if raw is None or not raw.strip():
        return default
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        raise ValueError(f"{name} must be a number") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return value


def floor_for_choice(choice: str | None, default: DropFloor) -> DropFloor:
    """The floor in effect for a user's stored choice.

    ``None`` (never chosen) or an unknown value is the server default; ``"off"``
    lets every drop through; a percentage preset keeps the default absolute part.
    """
    if choice == "off":
        return NO_DROP_FLOOR
    if choice in MIN_DROP_CHOICES:
        return DropFloor(Decimal(choice), default.absolute)
    return default


def clears_drop_floor(*, old: Decimal, new: Decimal, floor: DropFloor) -> bool:
    """Whether the drop from ``old`` to ``new`` is large enough to be announced."""
    if new >= old:
        return False
    drop = old - new
    if drop < floor.absolute:
        return False
    if floor.percent == 0:
        return True
    if old <= 0:
        return False
    return drop * 100 >= floor.percent * old
