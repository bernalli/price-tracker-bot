"""Hand-computed examples for the time-weighted price statistics.

Every expected value below is worked out by hand from the definition: each
reading holds its price from its own instant until the next reading, until
``max_hold`` elapses, or until ``now``, whichever comes first; only the part of
that hold inside ``[now - window, now)`` counts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

from price_tracker.core.pricestats import (
    DEFAULT_MIN_COVERAGE,
    Coverage,
    InsufficientHistory,
    PriceStats,
    Reading,
    price_context,
)

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _reading(at: datetime, price: str | int) -> Reading:
    return Reading(at, Decimal(price))


def _stats(result: PriceStats | InsufficientHistory) -> PriceStats:
    assert isinstance(result, PriceStats), result
    return result


def test_time_weighted_median_differs_from_count_median() -> None:
    window = 10 * HOUR
    start = NOW - window
    readings = [_reading(start, 100)] + [
        _reading(start + 9 * HOUR + timedelta(minutes=6 * j), 50) for j in range(10)
    ]
    stats = _stats(price_context(readings, now=NOW, window=window, max_hold=12 * HOUR))
    assert stats.coverage.covered == 10 * HOUR
    assert stats.coverage.readings_used == 11
    assert stats.coverage.gaps == 0
    assert stats.median == Decimal(100)
    assert stats.minimum == Decimal(50)
    assert stats.current == Decimal(50)


def test_exact_half_weight_takes_the_lower_price() -> None:
    window = 2 * HOUR
    start = NOW - window
    readings = [_reading(start, 20), _reading(start + HOUR, 10)]
    stats = _stats(price_context(readings, now=NOW, window=window, max_hold=2 * HOUR))
    assert stats.low == Decimal(10)
    assert stats.median == Decimal(10)
    assert stats.high == Decimal(20)


def test_carry_in_from_before_the_window() -> None:
    window = 3 * HOUR
    start = NOW - window
    readings = [_reading(start - HOUR, 10)]
    stats = _stats(price_context(readings, now=NOW, window=window, max_hold=3 * HOUR))
    assert stats.coverage.covered == 2 * HOUR
    assert stats.coverage.ratio == Fraction(2, 3)
    assert stats.coverage.readings_used == 1
    assert stats.coverage.gaps == 1
    ten = Decimal(10)
    assert (stats.low, stats.median, stats.high) == (ten, ten, ten)
    assert (stats.minimum, stats.maximum) == (ten, ten)
    assert stats.current is None


def test_hold_cap_creates_a_gap() -> None:
    window = 10 * HOUR
    start = NOW - window
    readings = [_reading(start, 10), _reading(start + 8 * HOUR, 12)]
    stats = _stats(price_context(readings, now=NOW, window=window, max_hold=3 * HOUR))
    assert stats.coverage.covered == 5 * HOUR
    assert stats.coverage.ratio == Fraction(1, 2)
    assert stats.coverage.gaps == 1
    assert stats.coverage.readings_used == 2
    assert stats.median == Decimal(10)
    assert stats.high == Decimal(12)
    assert stats.current == Decimal(12)


def test_reading_at_now_weighs_zero_but_is_current() -> None:
    window = HOUR
    start = NOW - window
    readings = [_reading(start, 10), _reading(NOW, 12)]
    stats = _stats(price_context(readings, now=NOW, window=window, max_hold=HOUR))
    assert stats.coverage.covered == HOUR
    assert stats.coverage.readings_used == 1
    assert stats.coverage.gaps == 0
    ten = Decimal(10)
    assert (stats.low, stats.median, stats.high, stats.minimum, stats.maximum) == (ten,) * 5
    assert stats.current == Decimal(12)


def test_stale_last_reading_is_not_current() -> None:
    window = 2 * HOUR
    start = NOW - window
    stats = _stats(price_context([_reading(start, 10)], now=NOW, window=window, max_hold=HOUR))
    assert stats.coverage.covered == HOUR
    assert stats.current is None


def test_constant_price_collapses_every_statistic() -> None:
    window = 3 * HOUR
    start = NOW - window
    readings = [_reading(start + k * HOUR, 10) for k in range(3)]
    stats = _stats(price_context(readings, now=NOW, window=window, max_hold=2 * HOUR))
    ten = Decimal(10)
    assert (stats.low, stats.median, stats.high, stats.minimum, stats.maximum) == (ten,) * 5
    assert stats.current == ten
    assert stats.coverage.readings_used == 3
    assert stats.coverage.gaps == 0


def test_equal_values_with_different_exponents_share_weight_and_keep_oldest_repr() -> None:
    window = 3 * HOUR
    start = NOW - window
    readings = [
        Reading(start, Decimal("10")),
        Reading(start + HOUR, Decimal("10.00")),
        Reading(start + 2 * HOUR, Decimal("12")),
    ]
    stats = _stats(price_context(readings, now=NOW, window=window, max_hold=3 * HOUR))
    assert stats.low == stats.median == stats.minimum == Decimal(10)
    assert stats.high == stats.maximum == Decimal(12)
    assert str(stats.median) == "10"
    assert str(stats.minimum) == "10"


def test_empty_input_is_no_readings_not_an_error() -> None:
    result = price_context((), now=NOW, window=HOUR, max_hold=HOUR)
    assert result == InsufficientHistory(Coverage(HOUR, timedelta(0), 0, 1), "no_readings", None)


def test_all_readings_expired_is_no_readings() -> None:
    start = NOW - HOUR
    result = price_context([_reading(start - 2 * HOUR, 10)], now=NOW, window=HOUR, max_hold=HOUR)
    assert result == InsufficientHistory(Coverage(HOUR, timedelta(0), 0, 1), "no_readings", None)


def test_low_coverage_reports_coverage_and_current() -> None:
    window = 3 * HOUR
    start = NOW - window
    result = price_context(
        [_reading(start + 2 * HOUR, 10)], now=NOW, window=window, max_hold=2 * HOUR
    )
    assert isinstance(result, InsufficientHistory), result
    assert result.reason == "low_coverage"
    assert result.coverage == Coverage(3 * HOUR, HOUR, 1, 1)
    assert result.coverage.ratio == Fraction(1, 3)
    assert result.current == Decimal(10)


def test_timezones_are_compared_as_instants() -> None:
    window = 3 * HOUR
    start = NOW - window
    plus_two = timezone(timedelta(hours=2))
    utc_readings = [_reading(start + k * HOUR, 10 + k) for k in range(3)]
    shifted = [Reading(r.at.astimezone(plus_two), r.price) for r in utc_readings]
    assert shifted[0].at.utcoffset() == timedelta(hours=2)
    expected = price_context(utc_readings, now=NOW, window=window, max_hold=2 * HOUR)
    result = price_context(shifted, now=NOW, window=window, max_hold=2 * HOUR)
    assert isinstance(expected, PriceStats)
    assert result == expected


def test_min_coverage_boundary_is_inclusive() -> None:
    window = 2 * HOUR
    start = NOW - window
    readings = [_reading(start, 10)]
    accepted = _stats(price_context(readings, now=NOW, window=window, max_hold=HOUR))
    assert accepted.coverage.ratio == Fraction(1, 2) == DEFAULT_MIN_COVERAGE
    rejected = price_context(
        readings, now=NOW, window=window, max_hold=HOUR, min_coverage=Fraction(2, 3)
    )
    assert isinstance(rejected, InsufficientHistory), rejected
    assert rejected.reason == "low_coverage"


def test_weights_are_microsecond_exact() -> None:
    window = timedelta(seconds=3)
    start = NOW - window
    readings = [
        _reading(start, 10),
        _reading(start + timedelta(seconds=1, microseconds=400_000), 12),
    ]
    stats = _stats(price_context(readings, now=NOW, window=window, max_hold=window))
    assert stats.coverage.covered == timedelta(seconds=3)
    assert stats.coverage.ratio == Fraction(1)
    assert stats.low == Decimal(10)
    assert stats.median == Decimal(12)
