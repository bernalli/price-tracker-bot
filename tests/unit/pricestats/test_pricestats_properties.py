"""Property tests for the time-weighted price statistics.

The reference is ``_oracle``: it samples the window one grid unit at a time and
asks which reading is in force at each sampled instant, then takes order
statistics by index over the sampled prices. It shares nothing with the
interval arithmetic of the module under test except the ``Reading`` type, and
it is exact whenever every instant and every duration is a multiple of the
unit, which the strategies below guarantee.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.pricestats import (
    MAX_PRICE,
    InsufficientHistory,
    PriceStats,
    Reading,
    price_context,
)

ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)
SECOND = timedelta(seconds=1)
MICROSECOND = timedelta(microseconds=1)
UNITS = (SECOND, MICROSECOND)

SMALL_PRICES = tuple(
    Decimal(text) for text in ("9.99", "10", "10.00", "12.5", "1000000000", "0.0001")
)
PRICES = st.one_of(
    st.sampled_from(SMALL_PRICES),
    st.decimals(
        min_value=Decimal("0.0001"),
        max_value=MAX_PRICE,
        allow_nan=False,
        allow_infinity=False,
    ),
)
MIN_COVERAGES = st.integers(1, 8).flatmap(
    lambda den: st.integers(1, den).map(lambda num: Fraction(num, den))
)

Result = PriceStats | InsufficientHistory


@dataclass(frozen=True)
class Scenario:
    readings: tuple[Reading, ...]
    now: datetime
    window: timedelta
    max_hold: timedelta
    min_coverage: Fraction
    unit: timedelta


@st.composite
def scenarios(
    draw: st.DrawFn, unit: timedelta, prices: st.SearchStrategy[Decimal] = PRICES
) -> Scenario:
    now_steps = draw(st.integers(0, 10_000))
    steps = sorted(draw(st.lists(st.integers(0, now_steps), unique=True, max_size=8)))
    readings = tuple(Reading(ORIGIN + k * unit, draw(prices)) for k in steps)
    return Scenario(
        readings=readings,
        now=ORIGIN + now_steps * unit,
        window=draw(st.integers(1, 10_000)) * unit,
        max_hold=draw(st.integers(1, 10_000)) * unit,
        min_coverage=draw(MIN_COVERAGES),
        unit=unit,
    )


def _run(scenario: Scenario, readings: tuple[Reading, ...] | None = None) -> Result:
    return price_context(
        scenario.readings if readings is None else readings,
        now=scenario.now,
        window=scenario.window,
        max_hold=scenario.max_hold,
        min_coverage=scenario.min_coverage,
    )


@dataclass(frozen=True)
class Expected:
    covered: timedelta
    readings_used: int
    gaps: int
    reason: str | None
    low: Decimal | None
    median: Decimal | None
    high: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None
    current: Decimal | None


def _oracle(
    readings: tuple[Reading, ...],
    now: datetime,
    window: timedelta,
    max_hold: timedelta,
    min_coverage: Fraction,
    *,
    unit: timedelta,
) -> Expected:
    times = [(r.at - ORIGIN) // unit for r in readings]
    hold = max_hold // unit
    end = (now - ORIGIN) // unit
    start = end - window // unit

    def in_force(u: int) -> int | None:
        index = bisect.bisect_right(times, u) - 1
        if index < 0 or u >= times[index] + hold:
            return None
        return index

    prices: list[Decimal] = []
    used: set[int] = set()
    gaps = 0
    previous_uncovered = False
    for u in range(start, end):
        index = in_force(u)
        if index is None:
            if not previous_uncovered:
                gaps += 1
            previous_uncovered = True
        else:
            previous_uncovered = False
            used.add(index)
            prices.append(readings[index].price)

    now_index = in_force(end)
    current = None if now_index is None else readings[now_index].price
    n = len(prices)
    covered = n * unit
    if n == 0:
        reason: str | None = "no_readings"
    elif Fraction(n * (unit // MICROSECOND), window // MICROSECOND) < min_coverage:
        reason = "low_coverage"
    else:
        reason = None
    if n == 0:
        return Expected(covered, len(used), gaps, reason, None, None, None, None, None, current)
    ordered = sorted(prices)

    def quantile(k: int) -> Decimal:
        return ordered[(k * n + 3) // 4 - 1]

    return Expected(
        covered,
        len(used),
        gaps,
        reason,
        quantile(1),
        quantile(2),
        quantile(3),
        ordered[0],
        ordered[-1],
        current,
    )


@pytest.mark.parametrize("unit", UNITS, ids=["seconds", "microseconds"])
@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_matches_the_per_unit_oracle(unit: timedelta, data: st.DataObject) -> None:
    scenario = data.draw(scenarios(unit))
    result = _run(scenario)
    expected = _oracle(
        scenario.readings,
        scenario.now,
        scenario.window,
        scenario.max_hold,
        scenario.min_coverage,
        unit=unit,
    )
    assert result.coverage.window == scenario.window
    assert result.coverage.covered == expected.covered
    assert result.coverage.readings_used == expected.readings_used
    assert result.coverage.gaps == expected.gaps
    assert result.current == expected.current
    if expected.reason is None:
        assert isinstance(result, PriceStats), result
        assert result.low == expected.low
        assert result.median == expected.median
        assert result.high == expected.high
        assert result.minimum == expected.minimum
        assert result.maximum == expected.maximum
    else:
        assert isinstance(result, InsufficientHistory), result
        assert result.reason == expected.reason


@settings(max_examples=300, deadline=None)
@given(scenario=scenarios(SECOND))
def test_order_statistics_are_ordered_and_observed(scenario: Scenario) -> None:
    result = _run(scenario)
    inputs = [r.price for r in scenario.readings]
    if result.current is not None:
        assert result.current in inputs
    if isinstance(result, PriceStats):
        assert result.minimum <= result.low <= result.median <= result.high <= result.maximum
        for value in (result.low, result.median, result.high, result.minimum, result.maximum):
            assert value in inputs


@st.composite
def gapless_chains(draw: st.DrawFn) -> tuple[Scenario, Reading]:
    """A chain whose consecutive readings are closer than ``max_hold``, plus one
    extra reading repeating the price of reading ``i`` strictly inside
    ``(t_i, t_{i+1})``. The extra instant uses a microsecond offset, so a free
    instant always exists even when two readings are one second apart."""
    hold = draw(st.integers(2, 10_000))
    distances = draw(st.lists(st.integers(1, hold - 1), min_size=1, max_size=7))
    first = draw(st.integers(0, 10_000))
    steps = [first]
    for distance in distances:
        steps.append(steps[-1] + distance)
    readings = tuple(Reading(ORIGIN + k * SECOND, draw(PRICES)) for k in steps)
    now_steps = steps[-1] + draw(st.integers(0, 10_000))
    index = draw(st.integers(0, len(distances) - 1))
    offset = draw(st.integers(1, distances[index] * 1_000_000 - 1))
    extra = Reading(readings[index].at + offset * MICROSECOND, readings[index].price)
    scenario = Scenario(
        readings=readings,
        now=ORIGIN + now_steps * SECOND,
        window=draw(st.integers(1, 10_000)) * SECOND,
        max_hold=hold * SECOND,
        min_coverage=draw(MIN_COVERAGES),
        unit=SECOND,
    )
    return scenario, extra


def _same_statistics(left: Result, right: Result) -> None:
    assert type(left) is type(right)
    assert left.coverage.covered == right.coverage.covered
    assert left.current == right.current
    if isinstance(left, PriceStats):
        assert isinstance(right, PriceStats)
        assert (left.low, left.median, left.high) == (right.low, right.median, right.high)
        assert (left.minimum, left.maximum) == (right.minimum, right.maximum)


@settings(max_examples=300, deadline=None)
@given(chain=gapless_chains())
def test_duplicate_checks_do_not_move_the_statistics(chain: tuple[Scenario, Reading]) -> None:
    scenario, extra = chain
    with_extra = tuple(sorted((*scenario.readings, extra), key=lambda r: r.at))
    _same_statistics(_run(scenario), _run(scenario, with_extra))


@settings(max_examples=300, deadline=None)
@given(scenario=scenarios(SECOND), data=st.data())
def test_adding_a_reading_never_decreases_coverage(scenario: Scenario, data: st.DataObject) -> None:
    taken = {r.at for r in scenario.readings}
    span = (scenario.now - ORIGIN) // MICROSECOND
    free = data.draw(
        st.integers(0, span)
        .map(lambda us: ORIGIN + us * MICROSECOND)
        .filter(lambda at: at not in taken)
    )
    extra = Reading(free, data.draw(PRICES))
    widened = tuple(sorted((*scenario.readings, extra), key=lambda r: r.at))
    assert _run(scenario, widened).coverage.covered >= _run(scenario).coverage.covered


SCALABLE_PRICES = st.one_of(
    st.sampled_from(SMALL_PRICES[:4] + SMALL_PRICES[5:]),
    st.decimals(
        min_value=Decimal("0.0001"),
        max_value=MAX_PRICE / 3,
        allow_nan=False,
        allow_infinity=False,
        places=4,
    ),
)


@settings(max_examples=300, deadline=None)
@given(
    scenario=scenarios(SECOND, SCALABLE_PRICES),
    factor=st.sampled_from((Decimal(2), Decimal(3), Decimal("0.5"))),
)
def test_scale_equivariance(scenario: Scenario, factor: Decimal) -> None:
    scaled = tuple(Reading(r.at, r.price * factor) for r in scenario.readings)
    base = _run(scenario)
    result = _run(scenario, scaled)
    assert type(result) is type(base)
    assert result.coverage == base.coverage
    if base.current is None:
        assert result.current is None
    else:
        assert result.current == base.current * factor
    if isinstance(base, PriceStats):
        assert isinstance(result, PriceStats)
        for name in ("low", "median", "high", "minimum", "maximum"):
            assert getattr(result, name) == getattr(base, name) * factor, name


@settings(max_examples=300, deadline=None)
@given(
    scenario=scenarios(SECOND),
    shift=st.integers(-(10**12), 10**12).map(lambda us: us * MICROSECOND),
)
def test_time_translation_invariance(scenario: Scenario, shift: timedelta) -> None:
    moved = tuple(Reading(r.at + shift, r.price) for r in scenario.readings)
    result = price_context(
        moved,
        now=scenario.now + shift,
        window=scenario.window,
        max_hold=scenario.max_hold,
        min_coverage=scenario.min_coverage,
    )
    assert result == _run(scenario)


@settings(max_examples=300, deadline=None)
@given(scenario=scenarios(SECOND))
def test_readings_that_never_touch_the_window_are_irrelevant(scenario: Scenario) -> None:
    start = scenario.now - scenario.window
    kept = tuple(r for r in scenario.readings if r.at + scenario.max_hold > start)
    assert _run(scenario, kept) == _run(scenario)


@pytest.mark.parametrize("unit", UNITS, ids=["seconds", "microseconds"])
@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_coverage_bounds(unit: timedelta, data: st.DataObject) -> None:
    coverage = _run(data.draw(scenarios(unit))).coverage
    assert 0 <= coverage.ratio <= 1
    assert timedelta(0) <= coverage.covered <= coverage.window
    assert coverage.gaps <= coverage.readings_used + 1
