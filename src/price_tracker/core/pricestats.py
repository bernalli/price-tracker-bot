"""Time-weighted price statistics over a window, with the coverage they rest on.

``price_context`` answers "what does this product usually cost, and how much of
the recent past do we actually know?" from the stored price readings of one
product. Each reading holds its price from its own instant until the next
reading, until ``max_hold`` has elapsed, or until ``now``, whichever comes
first; a stretch of time that no reading holds is a gap, a period in which
nobody knows the price. Statistics are weighted by how long each price held
inside the window, never by how many times it was read, so repeated checks at
an unchanged price do not move them.

Every statistic returned is a price that was actually observed: the lower
quartile, median and upper quartile (weighted, lower-quantile convention), the
minimum, the maximum and the current price. Weights are whole microseconds and
quantiles are compared in integer arithmetic, so results are exact and
repeatable. The coverage (window, covered time, readings used, gaps) is part of
every result, including the one that says the history is insufficient, so
"no band" and "a band computed from two hours of data" stay distinguishable.

Scope and contract:

- This is a nucleus that nothing calls yet. The only intended consumer is the
  single history loader that a later change adds; that loader turns stored rows
  into ``Reading`` objects (timezone-aware instants, prices filtered to the
  display domain, invalid rows counted), sorts them by instant, collapses equal
  instants, and takes ``now`` after reading the rows.
- Inputs are trusted and are never repaired: a malformed argument raises
  ``TypeError`` or ``ValueError`` whose first argument is a stable code. An
  empty or uncovered history is an outcome (``InsufficientHistory``), not an
  error. The function never sorts and never deduplicates.
- The module does not compute means or standard deviations, does not detect
  anomalies (the outlier filter stays where it is), does not compare stores, and
  has no notion of currency: all readings belong to one product, hence to one
  currency.
- It depends on the standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_PRICE: Final = Decimal(10) ** 9  # equals core.money.MAX_AMOUNT; a test pins the two together
DEFAULT_MIN_COVERAGE: Final = Fraction(1, 2)
USUAL_LOW_Q: Final = Fraction(1, 4)
MEDIAN_Q: Final = Fraction(1, 2)
USUAL_HIGH_Q: Final = Fraction(3, 4)
INSUFFICIENT_REASONS: Final = frozenset({"no_readings", "low_coverage"})

_MICROSECOND: Final = timedelta(microseconds=1)
_ZERO: Final = timedelta(0)


def _check_instant(value: object) -> None:
    """Accept a timezone-aware ``datetime`` (subclasses included), reject anything else."""
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive_datetime")


@dataclass(frozen=True, slots=True)
class Reading:
    """One accepted price observation of one product.

    ``at`` is timezone-aware; ``price`` is a plain finite ``Decimal`` with
    ``0 < price <= MAX_PRICE``. A reading that violates this cannot be built.
    """

    at: datetime
    price: Decimal

    def __post_init__(self) -> None:
        _check_instant(self.at)
        if type(self.price) is not Decimal:
            raise TypeError(f"price must be a Decimal, got {type(self.price).__name__}")
        # Finiteness first: comparing a signalling NaN raises InvalidOperation.
        if not self.price.is_finite():
            raise ValueError("non_finite_price")
        if not 0 < self.price <= MAX_PRICE:
            raise ValueError("price_out_of_range")


@dataclass(frozen=True, slots=True)
class Coverage:
    """How much of the window the readings hold.

    ``covered`` is the total held time inside the window, ``readings_used`` the
    number of readings with a positive share of it, ``gaps`` the number of
    maximal stretches of the window that no reading holds.
    """

    window: timedelta
    covered: timedelta
    readings_used: int
    gaps: int

    def __post_init__(self) -> None:
        if self.window <= _ZERO or not _ZERO <= self.covered <= self.window:
            raise ValueError("bad_coverage")

    @property
    def ratio(self) -> Fraction:
        """Covered share of the window, exact, in ``[0, 1]``."""
        return Fraction(self.covered // _MICROSECOND, self.window // _MICROSECOND)


@dataclass(frozen=True, slots=True)
class PriceStats:
    """Statistics over a sufficiently covered window; every price is an observed one."""

    coverage: Coverage
    low: Decimal
    median: Decimal
    high: Decimal
    minimum: Decimal
    maximum: Decimal
    current: Decimal | None

    def __post_init__(self) -> None:
        if not self.minimum <= self.low <= self.median <= self.high <= self.maximum:
            raise ValueError("unordered_statistics")


@dataclass(frozen=True, slots=True)
class InsufficientHistory:
    """The window is not covered enough to describe the usual price."""

    coverage: Coverage
    reason: str
    current: Decimal | None

    def __post_init__(self) -> None:
        if self.reason not in INSUFFICIENT_REASONS:
            raise ValueError("bad_reason")


def _check_positive_span(value: object, code: str) -> timedelta:
    if type(value) is not timedelta or value <= _ZERO:
        raise ValueError(code)
    return value


def _validated(readings: Iterable[Reading], now: datetime) -> tuple[Reading, ...]:
    """Materialise ``readings`` once and check type, strict order and no future instant."""
    items = tuple(readings)
    previous: datetime | None = None
    for item in items:
        if type(item) is not Reading:
            raise TypeError(f"expected a Reading, got {type(item).__name__}")
        if previous is not None:
            if item.at == previous:
                raise ValueError("duplicate_timestamp")
            if item.at < previous:
                raise ValueError("unsorted")
        if item.at > now:
            raise ValueError("future_reading")
        previous = item.at
    return items


def _quantile(groups: list[tuple[Decimal, int]], total: int, q: Fraction) -> Decimal:
    """Smallest price whose cumulative weight reaches ``q`` of ``total``."""
    cumulative = 0
    for price, weight in groups[:-1]:
        cumulative += weight
        if cumulative * q.denominator >= q.numerator * total:
            return price
    # The last group brings the cumulative weight to the total, which reaches any q <= 1.
    return groups[-1][0]


def price_context(
    readings: Iterable[Reading],
    *,
    now: datetime,
    window: timedelta,
    max_hold: timedelta,
    min_coverage: Fraction = DEFAULT_MIN_COVERAGE,
) -> PriceStats | InsufficientHistory:
    """Describe the price of one product over ``[now - window, now)``.

    ``readings`` must be strictly increasing in time and none may be later than
    ``now``; it is consumed exactly once. Returns ``InsufficientHistory`` when no
    reading holds any part of the window (``no_readings``) or when the covered
    share is below ``min_coverage`` (``low_coverage``), ``PriceStats`` otherwise.
    """
    _check_instant(now)
    _check_positive_span(window, "bad_window")
    try:
        start = now - window
    except OverflowError:
        raise ValueError("bad_window") from None
    _check_positive_span(max_hold, "bad_max_hold")
    if type(min_coverage) is not Fraction or not 0 < min_coverage <= 1:
        raise ValueError("bad_min_coverage")
    items = _validated(readings, now)

    # Integer microsecond offsets from the window start; the window is [0, end).
    end = window // _MICROSECOND
    hold = max_hold // _MICROSECOND
    offsets = [(item.at - start) // _MICROSECOND for item in items]

    groups: dict[Decimal, int] = {}
    covered = 0
    used = 0
    gaps = 0
    cursor = 0
    for index, item in enumerate(items):
        begin = offsets[index]
        next_begin = offsets[index + 1] if index + 1 < len(items) else end
        held_until = min(next_begin, begin + hold, end)
        lower = max(begin, 0)
        weight = held_until - lower
        if weight <= 0:
            continue
        if lower > cursor:
            gaps += 1
        cursor = held_until
        covered += weight
        used += 1
        groups[item.price] = groups.get(item.price, 0) + weight
    if cursor < end:
        gaps += 1

    current: Decimal | None = None
    if items and now - items[-1].at < max_hold:
        current = items[-1].price

    coverage = Coverage(window, covered * _MICROSECOND, used, gaps)
    if covered == 0:
        return InsufficientHistory(coverage, "no_readings", current)
    if coverage.ratio < min_coverage:
        return InsufficientHistory(coverage, "low_coverage", current)

    ordered = sorted(groups.items())
    return PriceStats(
        coverage=coverage,
        low=_quantile(ordered, covered, USUAL_LOW_Q),
        median=_quantile(ordered, covered, MEDIAN_Q),
        high=_quantile(ordered, covered, USUAL_HIGH_Q),
        minimum=ordered[0][0],
        maximum=ordered[-1][0],
        current=current,
    )
