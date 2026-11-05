"""The minimum drop a price alert needs, on top of the product's own rule."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from price_tracker.core.alert import crosses_threshold, should_alert
from price_tracker.core.drop_floor import (
    DEFAULT_DROP_FLOOR,
    NO_DROP_FLOOR,
    DropFloor,
    clears_drop_floor,
    floor_for_choice,
    parse_floor_value,
)

D = Decimal


def _alert(
    old: str,
    new: str,
    *,
    rule: str = "any_drop",
    value: str = "0",
    target: str | None = None,
    floor: DropFloor = DEFAULT_DROP_FLOOR,
) -> bool:
    return should_alert(
        old=D(old),
        new=D(new),
        threshold_type=rule,  # type: ignore[arg-type]
        threshold_value=D(value),
        target_price=None if target is None else D(target),
        floor=floor,
    )


def test_a_few_cents_off_an_expensive_product_is_not_announced() -> None:
    assert not _alert("199.49", "199.00")


def test_a_real_drop_is_announced() -> None:
    assert _alert("199.49", "189.00")


def test_switching_the_floor_off_restores_every_drop() -> None:
    assert _alert("199.49", "199.00", floor=NO_DROP_FLOOR)
    assert _alert("199.49", "199.48", floor=NO_DROP_FLOOR)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("100", "98", True),  # 2 % and 2: both parts met exactly
        ("100", "98.01", False),  # 1.99 %
        ("40", "39", True),  # 2.5 % and 1: both hold
        ("10", "9.50", False),  # 5 % but only 0.50
        ("1000", "990.01", False),  # 9.99 but under 1 %
        ("50", "49", True),  # 2 % and exactly 1
    ],
)
def test_both_parts_of_the_default_floor_must_hold(old: str, new: str, expected: bool) -> None:
    assert _alert(old, new) is expected


@pytest.mark.parametrize("rule", ["percentage", "absolute", "target", "any_drop"])
def test_the_floor_applies_to_every_rule(rule: str) -> None:
    value = {"percentage": "0.1", "absolute": "0.10", "target": "199.10", "any_drop": "0"}[rule]
    assert crosses_threshold(
        old=D("199.49"),
        new=D("199.00"),
        threshold_type=rule,  # type: ignore[arg-type]
        threshold_value=D(value),
    )
    assert not _alert("199.49", "199.00", rule=rule, value=value)


def test_a_target_crossing_still_needs_the_floor() -> None:
    assert not _alert("199.49", "199.00", rule="percentage", value="50", target="199.20")
    assert _alert("199.49", "189.00", rule="percentage", value="50", target="190.00")


def test_the_rule_still_applies_on_top_of_the_floor() -> None:
    # 10.49 off 199.49 clears the floor, but not a 10 % rule.
    assert not _alert("199.49", "189.00", rule="percentage", value="10")
    assert not _alert("199.49", "189.00", rule="absolute", value="20")
    assert not _alert("199.49", "189.00", rule="target", value="150")


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        (None, DEFAULT_DROP_FLOOR),
        ("off", NO_DROP_FLOOR),
        ("1", DropFloor(D(1), D(1))),
        ("2", DropFloor(D(2), D(1))),
        ("5", DropFloor(D(5), D(1))),
        ("7", DEFAULT_DROP_FLOOR),
        ("", DEFAULT_DROP_FLOOR),
    ],
)
def test_a_stored_choice_picks_the_floor(choice: str | None, expected: DropFloor) -> None:
    assert floor_for_choice(choice, DEFAULT_DROP_FLOOR) == expected


def test_a_preset_keeps_the_configured_absolute_part() -> None:
    default = DropFloor(D(3), D("2.5"))
    assert floor_for_choice("5", default) == DropFloor(D(5), D("2.5"))
    assert floor_for_choice(None, default) == default


@pytest.mark.parametrize("bad", [D(-1), D("NaN"), D("Infinity"), 2])
def test_an_unusable_floor_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="percent"):
        DropFloor(bad, D(1))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="absolute"):
        DropFloor(D(1), bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, D(2)), ("", D(2)), (" 5 ", D(5)), ("0", D(0)), ("1.5", D("1.5"))]
)
def test_floor_values_from_the_environment(raw: str | None, expected: Decimal) -> None:
    assert parse_floor_value(raw, name="MIN_DROP_PERCENT", default=D(2)) == expected


@pytest.mark.parametrize("raw", ["-1", "NaN", "inf", "two", "1,5"])
def test_unusable_floor_values_fail_at_startup(raw: str) -> None:
    with pytest.raises(ValueError, match="MIN_DROP_PERCENT"):
        parse_floor_value(raw, name="MIN_DROP_PERCENT", default=D(2))


def test_a_rise_or_a_zero_old_price_never_clears_a_floor() -> None:
    assert not clears_drop_floor(old=D(10), new=D(10), floor=NO_DROP_FLOOR)
    assert not clears_drop_floor(old=D(10), new=D(11), floor=NO_DROP_FLOOR)
    assert not clears_drop_floor(old=D(0), new=D(-5), floor=DEFAULT_DROP_FLOOR)
    assert clears_drop_floor(old=D(0), new=D(-5), floor=DropFloor(D(0), D(1)))


# ── Property: the floor suppresses exactly the drops below it ───────

_prices = st.decimals(min_value=D("0.01"), max_value=D("100000"), places=2)
_rules = st.sampled_from(["percentage", "absolute", "target", "any_drop"])
_floors = st.builds(
    DropFloor,
    percent=st.sampled_from([D(0), D(1), D(2), D(5), D("2.5")]),
    absolute=st.sampled_from([D(0), D(1), D("0.5"), D(10)]),
)


@given(
    old=_prices,
    new=_prices,
    rule=_rules,
    value=_prices,
    target=st.none() | _prices,
    floor=_floors,
)
def test_floor_property(
    old: Decimal,
    new: Decimal,
    rule: str,
    value: Decimal,
    target: Decimal | None,
    floor: DropFloor,
) -> None:
    """No alert below the floor; the floor never hides a drop above both of its parts."""
    rule_hit = crosses_threshold(
        old=old,
        new=new,
        threshold_type=rule,  # type: ignore[arg-type]
        threshold_value=value,
    ) or (target is not None and new <= target < old)
    alerted = should_alert(
        old=old,
        new=new,
        threshold_type=rule,  # type: ignore[arg-type]
        threshold_value=value,
        target_price=target,
        floor=floor,
    )
    drop = old - new
    above_both = drop > 0 and drop >= floor.absolute and drop * 100 >= floor.percent * old
    if alerted:
        assert rule_hit
        assert drop >= floor.absolute
        assert drop * 100 >= floor.percent * old
    assert alerted == (rule_hit and above_both)
    # Without a floor the decision is exactly the rule's, as before the floor existed.
    without = should_alert(
        old=old,
        new=new,
        threshold_type=rule,  # type: ignore[arg-type]
        threshold_value=value,
        target_price=target,
        floor=NO_DROP_FLOOR,
    )
    assert without == rule_hit
