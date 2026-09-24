"""Malformed inputs are rejected with an exact code, never repaired.

Every negative case asserts the exception type and, for ``ValueError``, the
exact code in ``args[0]``; each group has a positive control on the same path
with a well-formed input.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from price_tracker.core import money, pricestats
from price_tracker.core.pricestats import (
    Coverage,
    InsufficientHistory,
    PriceStats,
    Reading,
    price_context,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

SRC_DIR = Path(__file__).resolve().parents[3] / "src"
MODULE_PATH = SRC_DIR / "price_tracker" / "core" / "pricestats.py"
TARGET = "price_tracker.core.pricestats"

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
PLUS_TWO = timezone(timedelta(hours=2))


class _DecimalSubclass(Decimal):
    pass


def _raises_code(code: str, call: Any, *args: Any, **kwargs: Any) -> None:
    with pytest.raises(ValueError) as excinfo:  # noqa: PT011 - the code is asserted exactly below
        call(*args, **kwargs)
    assert excinfo.value.args[0] == code


# --- the bound is mirrored, not imported --------------------------------------


def test_max_price_equals_money_max_amount() -> None:
    assert pricestats.MAX_PRICE == money.MAX_AMOUNT, (
        "MAX_PRICE mirrors core.money.MAX_AMOUNT; the bound is defined there: "
        "change money first, then this constant"
    )


# --- no caller, standard library only ------------------------------------------


def _imported_names(tree: ast.Module, package: tuple[str, ...]) -> set[str]:
    """Every module name an import statement in ``tree`` can bind, with relative
    imports resolved against ``package`` and ``from X import y`` also yielding
    ``X.y`` (``y`` may be a submodule)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = list(package[: len(package) - (node.level - 1)])
                if node.module is not None:
                    parts.append(node.module)
                base = ".".join(parts)
            else:
                base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _names_target(names: set[str]) -> bool:
    return any(name == TARGET or name.startswith(TARGET + ".") for name in names)


@pytest.mark.parametrize(
    "source",
    [
        "from price_tracker.core import pricestats",
        "from . import pricestats",
        "from .pricestats import PriceStats",
        "import price_tracker.core.pricestats",
    ],
)
def test_import_scanner_finds_every_import_form(source: str) -> None:
    assert _names_target(_imported_names(ast.parse(source), ("price_tracker", "core")))


def test_import_scanner_ignores_sibling_modules() -> None:
    source = "from price_tracker.core import money"
    assert not _names_target(_imported_names(ast.parse(source), ("price_tracker", "core")))


def test_pricestats_has_no_callers_and_imports_only_stdlib() -> None:
    """Deliberate tripwire; the change that wires the module replaces it with the
    list of allowed callers."""
    script = (
        "import sys\n"
        f"import {TARGET}\n"
        "print('\\n'.join(sorted(m for m in sys.modules "
        "if m == 'price_tracker' or m.startswith('price_tracker.'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["price_tracker", "price_tracker.core", TARGET]

    scanned = 0
    callers: list[str] = []
    for path in sorted((SRC_DIR / "price_tracker").rglob("*.py")):
        scanned += 1
        if path == MODULE_PATH:
            continue
        relative = path.relative_to(SRC_DIR)
        package = relative.parts[:-1]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _names_target(_imported_names(tree, package)):
            callers.append(str(relative))
    assert MODULE_PATH.exists()
    assert scanned > 50, scanned
    assert not callers, f"the price statistics module is not wired yet: {callers}"


# --- Reading --------------------------------------------------------------------


@pytest.mark.parametrize(
    "at",
    [date(2026, 1, 10), "2026-01-10 12:00:00", None],
    ids=["date", "str", "none"],
)
def test_reading_rejects_non_datetime_instant(at: Any) -> None:
    with pytest.raises(TypeError):
        Reading(at, Decimal(10))


def test_reading_rejects_naive_instant() -> None:
    _raises_code("naive_datetime", Reading, datetime(2026, 1, 10, 12), Decimal(10))


@pytest.mark.parametrize(
    "price",
    [10.0, 10, True, "10", _DecimalSubclass(10)],
    ids=["float", "int", "bool", "str", "decimal-subclass"],
)
def test_reading_rejects_non_decimal_price(price: Any) -> None:
    with pytest.raises(TypeError):
        Reading(NOW, price)


@pytest.mark.parametrize("text", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_reading_rejects_non_finite_price(text: str) -> None:
    _raises_code("non_finite_price", Reading, NOW, Decimal(text))


@pytest.mark.parametrize(
    "price",
    [Decimal(0), Decimal("-0"), Decimal("-5"), Decimal(10) ** 9 + Decimal("0.01")],
    ids=["zero", "negative-zero", "negative", "above-max"],
)
def test_reading_rejects_out_of_range_price(price: Decimal) -> None:
    _raises_code("price_out_of_range", Reading, NOW, price)


@pytest.mark.parametrize("price", [Decimal("0.0001"), Decimal(10) ** 9])
def test_reading_accepts_domain_bounds(price: Decimal) -> None:
    reading = Reading(NOW, price)
    assert reading.price is price
    assert reading.at is NOW


# --- price_context arguments ------------------------------------------------------


def _call(**overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "readings": (Reading(NOW - HOUR, Decimal(10)),),
        "now": NOW,
        "window": HOUR,
        "max_hold": HOUR,
    }
    arguments.update(overrides)
    readings = arguments.pop("readings")
    return price_context(readings, **arguments)


def test_well_formed_call_is_accepted() -> None:
    assert isinstance(_call(), PriceStats)
    assert isinstance(_call(min_coverage=Fraction(1)), PriceStats)


def test_rejects_naive_now() -> None:
    _raises_code("naive_datetime", _call, now=datetime(2026, 1, 10, 12))


def test_rejects_non_datetime_now() -> None:
    with pytest.raises(TypeError):
        _call(now=date(2026, 1, 10))


@pytest.mark.parametrize(
    "window",
    [timedelta(0), timedelta(seconds=-1), 0, timedelta.max],
    ids=["zero", "negative", "int", "overflowing"],
)
def test_rejects_bad_window(window: Any) -> None:
    _raises_code("bad_window", _call, window=window)


@pytest.mark.parametrize(
    "max_hold",
    [timedelta(0), timedelta(seconds=-1), 1.0],
    ids=["zero", "negative", "float"],
)
def test_rejects_bad_max_hold(max_hold: Any) -> None:
    _raises_code("bad_max_hold", _call, max_hold=max_hold)


def test_huge_max_hold_holds_until_now() -> None:
    start = NOW - HOUR
    result = _call(readings=(Reading(start, Decimal(10)),), max_hold=timedelta.max)
    assert isinstance(result, PriceStats)
    assert result.coverage.covered == HOUR
    assert result.current == Decimal(10)


@pytest.mark.parametrize(
    "min_coverage",
    [Fraction(0), Fraction(3, 2), 0.5, 1],
    ids=["zero", "above-one", "float", "int"],
)
def test_rejects_bad_min_coverage(min_coverage: Any) -> None:
    _raises_code("bad_min_coverage", _call, min_coverage=min_coverage)


@pytest.mark.parametrize(
    "element",
    [(NOW - HOUR, Decimal(10)), {"at": NOW - HOUR, "price": Decimal(10)}, None],
    ids=["tuple", "dict", "none"],
)
def test_rejects_non_reading_element(element: Any) -> None:
    with pytest.raises(TypeError):
        _call(readings=(Reading(NOW - 2 * HOUR, Decimal(10)), element))


@pytest.mark.parametrize("second_price", ["10", "11"], ids=["same-price", "other-price"])
def test_rejects_duplicate_timestamp(second_price: str) -> None:
    at = NOW - HOUR
    readings = (Reading(at, Decimal(10)), Reading(at, Decimal(second_price)))
    _raises_code("duplicate_timestamp", _call, readings=readings)


def test_same_instant_in_two_zones_is_a_duplicate() -> None:
    at = NOW - HOUR
    readings = (Reading(at, Decimal(10)), Reading(at.astimezone(PLUS_TWO), Decimal(11)))
    _raises_code("duplicate_timestamp", _call, readings=readings)


def test_rejects_unsorted_readings() -> None:
    readings = (Reading(NOW - HOUR, Decimal(10)), Reading(NOW - 2 * HOUR, Decimal(11)))
    _raises_code("unsorted", _call, readings=readings)


def test_rejects_future_reading() -> None:
    readings = (
        Reading(NOW - HOUR, Decimal(10)),
        Reading(NOW + timedelta(microseconds=1), Decimal(11)),
    )
    _raises_code("future_reading", _call, readings=readings)


def test_reading_at_now_is_not_future() -> None:
    readings = (Reading(NOW - HOUR, Decimal(10)), Reading(NOW, Decimal(11)))
    result = _call(readings=readings)
    assert isinstance(result, PriceStats)
    assert result.current == Decimal(11)


# --- iteration --------------------------------------------------------------------


def test_generator_is_consumed_once() -> None:
    window = 3 * HOUR
    start = NOW - window
    layout = tuple(Reading(start + k * HOUR, Decimal(10)) for k in range(3))

    def generate() -> Iterator[Reading]:
        yield from layout

    expected = price_context(layout, now=NOW, window=window, max_hold=2 * HOUR)
    assert isinstance(expected, PriceStats)
    assert price_context(generate(), now=NOW, window=window, max_hold=2 * HOUR) == expected


# --- result types -----------------------------------------------------------------


def _coverage() -> Coverage:
    return Coverage(HOUR, HOUR, 1, 0)


def test_result_types_accept_well_formed_construction() -> None:
    ten = Decimal(10)
    stats = PriceStats(_coverage(), ten, ten, Decimal(11), ten, Decimal(12), None)
    assert stats.coverage.ratio == Fraction(1)
    for reason in pricestats.INSUFFICIENT_REASONS:
        assert (
            InsufficientHistory(Coverage(HOUR, timedelta(0), 0, 1), reason, None).reason == reason
        )
    assert Coverage(HOUR, timedelta(0), 0, 1).ratio == Fraction(0)


def test_result_types_reject_malformed_construction() -> None:
    _raises_code("bad_reason", InsufficientHistory, _coverage(), "other", None)
    _raises_code(
        "unordered_statistics",
        PriceStats,
        _coverage(),
        Decimal(11),
        Decimal(10),
        Decimal(12),
        Decimal(9),
        Decimal(13),
        None,
    )
    _raises_code("bad_coverage", Coverage, HOUR, 2 * HOUR, 1, 0)


@pytest.mark.parametrize(
    "fields",
    [
        # minimum above low
        (Decimal(11), Decimal(12), Decimal(13), Decimal(12), Decimal(14)),
        # median above high
        (Decimal(10), Decimal(13), Decimal(12), Decimal(9), Decimal(14)),
        # high above maximum
        (Decimal(10), Decimal(11), Decimal(15), Decimal(9), Decimal(14)),
    ],
    ids=["minimum-above-low", "median-above-high", "high-above-maximum"],
)
def test_price_stats_rejects_every_broken_order(fields: tuple[Decimal, ...]) -> None:
    low, median, high, minimum, maximum = fields
    _raises_code(
        "unordered_statistics", PriceStats, _coverage(), low, median, high, minimum, maximum, None
    )


@pytest.mark.parametrize(
    ("window", "covered"),
    [(HOUR, timedelta(microseconds=-1)), (timedelta(0), timedelta(0))],
    ids=["negative-covered", "empty-window"],
)
def test_coverage_rejects_malformed_spans(window: timedelta, covered: timedelta) -> None:
    _raises_code("bad_coverage", Coverage, window, covered, 0, 1)
