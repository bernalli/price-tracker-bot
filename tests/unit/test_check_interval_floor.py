"""Pure rules of the floor between two scheduled checks of one product.

A scraper may declare ``min_check_interval_minutes``: no product interval, own or
global, lowers it, and it counts from the latest readable anchor (this process's
own attempt, the stored read, the stored failure, the product's creation).
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.registry import ScraperRegistry, discover_builtin_scrapers
from price_tracker.core.scheduler_common import (
    CHECK_TICK_MINUTES,
    MAX_INTERVAL_MINUTES,
    configured_interval_minutes,
    effective_interval_minutes,
    floor_anchor,
    floor_holds,
    interval_floor_minutes,
)
from price_tracker.core.scraper_base import AbstractScraper
from price_tracker.db.models import ProductRecord
from price_tracker.scrapers.shopify import ShopifyScraper

TICK = CHECK_TICK_MINUTES
MAX = MAX_INTERVAL_MINUTES
FMT = "%Y-%m-%d %H:%M:%S"


def old(own: object, g: int) -> int:
    """The interval rule before the floor existed, copied verbatim."""
    if isinstance(own, int) and not isinstance(own, bool) and 1 <= own <= MAX:
        return own
    return min(max(g, TICK), MAX)


_OWN = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(10**7), 10**8),
    st.floats(allow_nan=True),
    st.text(max_size=5),
)


@given(own=_OWN, g=st.integers(-(10**6), 10**8))
@settings(max_examples=200, deadline=None)
def test_without_a_floor_the_interval_is_unchanged(own: object, g: int) -> None:
    assert effective_interval_minutes(own, global_minutes=g) == old(own, g)


@given(own=_OWN, g=st.integers(-(10**6), 10**8), floor=st.integers(0, MAX))
@settings(max_examples=200, deadline=None)
def test_the_floor_is_never_undercut(own: object, g: int, floor: int) -> None:
    result = effective_interval_minutes(own, global_minutes=g, floor_minutes=floor)
    assert result == max(old(own, g), floor)
    assert result >= floor
    assert result <= MAX


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, 0),
        (False, 0),
        (-1, 0),
        ("360", 0),
        (360.0, 0),
        (None, 0),
        (MagicMock(), 0),
        (0, 0),
        (360, 360),
        (10**9, MAX),
    ],
    ids=[
        "true",
        "false",
        "negative",
        "text",
        "float",
        "none",
        "mock",
        "zero",
        "360",
        "huge",
    ],
)
def test_floor_attribute_values(value: object, expected: int) -> None:
    assert interval_floor_minutes(SimpleNamespace(min_check_interval_minutes=value)) == expected


def test_floor_of_no_scraper_or_no_attribute_is_zero() -> None:
    assert interval_floor_minutes(None) == 0
    assert interval_floor_minutes(object()) == 0


def test_only_shopify_declares_a_floor() -> None:
    assert ShopifyScraper.min_check_interval_minutes == 360
    assert AbstractScraper.min_check_interval_minutes == 0
    registry = ScraperRegistry()
    discover_builtin_scrapers(registry)
    floors = {scraper.name: interval_floor_minutes(scraper) for scraper in registry}
    assert floors.pop("shopify") == 360
    assert floors
    assert set(floors.values()) == {0}


def test_a_shopify_subclass_inherits_the_floor_whatever_the_registration_order() -> None:
    class ShopifyChild(ShopifyScraper):
        name: ClassVar[str] = "shopify_child"
        priority: ClassVar[int] = 81

    registry = ScraperRegistry()
    registry.register(ShopifyChild())
    registry.register(ShopifyScraper())

    chosen = registry.resolve("https://shop.example.com/products/widget")

    assert isinstance(chosen, ShopifyChild)
    assert interval_floor_minutes(chosen) == 360


# --- the stored global interval ---------------------------------------------

CONFIGURED = 360


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        (None, CONFIGURED),
        ("", CONFIGURED),
        ("   ", CONFIGURED),
        (" 60 ", CONFIGURED),
        ("-60", CONFIGURED),
        ("1.5", CONFIGURED),
        ("12a", CONFIGURED),
        ("6", TICK + 1),
        ("3", TICK),
        ("0", TICK),
        ("²", CONFIGURED),
        ("٣٦٠", CONFIGURED),
        (True, CONFIGURED),
        (False, CONFIGURED),
        (60, CONFIGURED),
        (b"60", CONFIGURED),
        (["60"], CONFIGURED),
        ({"60": 60}, CONFIGURED),
        ("9" * 5000, MAX),
        ("0" * 5000 + "7", 7),
        (str(MAX), MAX),
        (str(MAX + 1), MAX),
    ],
    ids=[
        "none",
        "empty",
        "spaces",
        "padded",
        "negative",
        "fraction",
        "truncated",
        "six",
        "three",
        "zero",
        "superscript",
        "arabic-digits",
        "true",
        "false",
        "int",
        "bytes",
        "list",
        "mapping",
        "huge",
        "leading-zeros",
        "max",
        "max-plus-one",
    ],
)
def test_configured_interval_never_raises(saved: object, expected: int) -> None:
    assert configured_interval_minutes(saved, configured=CONFIGURED) == expected


@pytest.mark.parametrize(
    ("configured", "expected"), [(1, TICK), (-5, TICK), (10**9, MAX), (90, 90)]
)
def test_the_configured_fallback_is_clamped(configured: int, expected: int) -> None:
    assert configured_interval_minutes(None, configured=configured) == expected


_SAVED = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True),
    st.binary(max_size=10),
    st.text(max_size=40),
    st.text(alphabet="0123456789", min_size=1, max_size=40),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
)


@given(saved=_SAVED, configured=st.integers(-(10**6), 10**9))
@settings(max_examples=200, deadline=None)
def test_configured_interval_is_always_a_bounded_integer(saved: object, configured: int) -> None:
    result = configured_interval_minutes(saved, configured=configured)
    assert type(result) is int
    assert TICK <= result <= MAX
    fallback = min(max(configured, TICK), MAX)
    ascii_digits = isinstance(saved, str) and saved != "" and all(c in "0123456789" for c in saved)
    if not ascii_digits:
        assert result == fallback
    else:
        assert isinstance(saved, str)
        assert result == min(max(int(saved), TICK), MAX)


# --- floor anchor ------------------------------------------------------------


def _record(
    *,
    last_checked_at: object = None,
    last_error_at: object = None,
    created_at: object = None,
) -> ProductRecord:
    return ProductRecord(
        id=1,
        user_id=1,
        url="https://shop.example.com/products/widget",
        name="Widget",
        domain="example.com",
        initial_price=None,
        current_price=None,
        lowest_price=None,
        highest_price=None,
        target_price=None,
        threshold_type="percentage",
        threshold_value=Decimal("10"),
        is_active=True,
        is_available=True,
        consecutive_errors=0,
        currency="EUR",
        check_interval_minutes=None,
        last_checked_at=last_checked_at,  # type: ignore[arg-type]
        last_notified_at=None,
        last_error_at=last_error_at,  # type: ignore[arg-type]
        created_at=created_at,  # type: ignore[arg-type]
    )


NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).strftime(FMT)


def test_no_anchor_means_the_floor_cannot_hold() -> None:
    product = _record()
    assert floor_anchor(product, attempted_at=None) is None
    assert not floor_holds(product, floor_minutes=360, attempted_at=None, now=NOW)


def test_creation_alone_is_an_anchor() -> None:
    assert floor_holds(_record(created_at=_ago(10)), floor_minutes=360, attempted_at=None, now=NOW)
    assert not floor_holds(
        _record(created_at=_ago(361)), floor_minutes=360, attempted_at=None, now=NOW
    )


def test_the_newest_anchor_wins_whatever_its_kind() -> None:
    product = _record(last_checked_at=_ago(500), last_error_at=_ago(100), created_at=_ago(1000))
    attempted = NOW - timedelta(minutes=400)

    anchor = floor_anchor(product, attempted_at=attempted)

    assert anchor == NOW - timedelta(minutes=100)


_UNREADABLE: list[object] = [
    "",
    "garbage",
    "2026-01-0",
    "2026-01-02 03:04:05+25:00",
    5,
    b"x",
    [],
    {},
    True,
]


@pytest.mark.parametrize("bad", _UNREADABLE, ids=[f"bad{i}" for i in range(len(_UNREADABLE))])
@pytest.mark.parametrize("field", ["last_checked_at", "last_error_at", "created_at"])
def test_unreadable_values_are_skipped_beside_a_valid_anchor(bad: object, field: str) -> None:
    fields: dict[str, object] = {
        "last_checked_at": None,
        "last_error_at": None,
        "created_at": None,
    }
    fields[field] = bad
    valid = "last_error_at" if field != "last_error_at" else "last_checked_at"
    fields[valid] = _ago(100)
    product = _record(**fields)

    assert floor_anchor(product, attempted_at=None) == NOW - timedelta(minutes=100)
    assert floor_holds(product, floor_minutes=360, attempted_at=None, now=NOW)


@pytest.mark.parametrize(
    "attempted",
    [datetime(2026, 11, 13, 11, 0, 0), "2026-11-13 11:00:00", 5, None],
    ids=["naive", "text", "int", "none"],
)
def test_an_unusable_attempt_is_skipped(attempted: object) -> None:
    product = _record(created_at=_ago(1000), last_checked_at=_ago(100))

    assert floor_anchor(product, attempted_at=attempted) == NOW - timedelta(minutes=100)


def test_a_future_creation_holds_the_floor() -> None:
    product = _record(created_at=(NOW + timedelta(days=1)).strftime(FMT))
    assert floor_holds(product, floor_minutes=360, attempted_at=None, now=NOW)


@pytest.mark.parametrize("extreme", ["0001-01-01 00:00:00", "9999-12-31 23:59:59"])
def test_extreme_timestamps_do_not_raise(extreme: str) -> None:
    product = _record(created_at=extreme, last_checked_at=extreme, last_error_at=extreme)
    floor_holds(product, floor_minutes=360, attempted_at=None, now=NOW)
    floor_anchor(product, attempted_at=None)


@pytest.mark.parametrize("floor", [0, -1, -360])
def test_without_a_positive_floor_nothing_holds(floor: int) -> None:
    product = _record(created_at=_ago(1), last_checked_at=_ago(1))
    assert not floor_holds(
        product, floor_minutes=floor, attempted_at=NOW - timedelta(minutes=1), now=NOW
    )


# --- property: an independent oracle of readable anchors ----------------------


def _oracle_db_value(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


_VALUE = st.one_of(
    st.none(),
    st.text(max_size=30),
    st.integers(),
    st.binary(max_size=5),
    st.integers(-2000, 2000).map(lambda minutes: ("ts", minutes)),
)


def _as_db(value: object) -> object:
    if isinstance(value, tuple):
        return (NOW - timedelta(minutes=value[1])).strftime(FMT)
    return value


def _as_attempt(value: object) -> object:
    if isinstance(value, tuple):
        return NOW - timedelta(minutes=value[1])
    return value


@given(
    attempt=_VALUE,
    checked=_VALUE,
    error=_VALUE,
    created=_VALUE,
    floor=st.integers(-5, MAX),
)
@settings(max_examples=200, deadline=None)
def test_the_anchor_is_the_newest_readable_value(
    attempt: Any, checked: Any, error: Any, created: Any, floor: int
) -> None:
    attempted_at = _as_attempt(attempt)
    db_values = [_as_db(checked), _as_db(error), _as_db(created)]
    readable = [parsed for parsed in map(_oracle_db_value, db_values) if parsed is not None]
    if isinstance(attempted_at, datetime):
        readable.append(attempted_at)
    expected = max(readable) if readable else None

    for order in itertools.permutations(db_values):
        product = _record(last_checked_at=order[0], last_error_at=order[1], created_at=order[2])
        assert floor_anchor(product, attempted_at=attempted_at) == expected
        holds = floor_holds(product, floor_minutes=floor, attempted_at=attempted_at, now=NOW)
        assert holds == (
            floor > 0 and expected is not None and NOW - expected < timedelta(minutes=floor)
        )
