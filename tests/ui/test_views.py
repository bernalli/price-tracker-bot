"""Verifies price_tracker.app.views.ProductView construction and validation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, get_args

import pytest

from price_tracker.app.views import ProductStatus, ProductView, ThresholdType
from price_tracker.core.alert import ThresholdType as AlertThresholdType

_NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _make_view(**overrides: Any) -> ProductView:  # noqa: ANN401
    base: dict[str, Any] = {
        "id": 42,
        "name": "Widget",
        "url": "https://shop.example.com/item/42",
        "domain": "shop.example.com",
        "currency": "EUR",
        "current": Decimal("10"),
        "initial": Decimal("12"),
        "lowest": Decimal("9"),
        "target": Decimal("8"),
        "threshold_type": "percentage",
        "threshold_value": Decimal("10"),
        "status": "active",
        "consecutive_errors": 0,
        "check_interval_minutes": None,
        "default_interval_minutes": 360,
        "last_checked_at": _NOW,
        "reference_estimate": None,
        "reference_currency": "EUR",
    }
    base.update(overrides)
    return ProductView(**base)


def test_valid_construction() -> None:
    view = _make_view()
    assert view.id == 42
    assert view.status == "active"


def test_valid_construction_with_all_optionals_none() -> None:
    view = _make_view(
        current=None,
        initial=None,
        lowest=None,
        target=None,
        check_interval_minutes=None,
        last_checked_at=None,
        reference_estimate=None,
    )
    assert view.current is None
    assert view.last_checked_at is None


def test_threshold_type_matches_core_alert() -> None:
    assert get_args(ThresholdType) == get_args(AlertThresholdType)


def test_product_status_literals() -> None:
    assert get_args(ProductStatus) == ("active", "paused", "suspended")


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": 0},
        {"id": -1},
        {"currency": "eur"},
        {"currency": "XYZ"},
        {"currency": ""},
        {"current": Decimal("NaN")},
        {"current": Decimal("-1")},
        {"current": 1.0},
        {"threshold_type": "pct"},
        {"status": "on"},
        {"consecutive_errors": -1},
        {"check_interval_minutes": 0},
        {"default_interval_minutes": 0},
        {"last_checked_at": datetime(2026, 1, 1)},
        {"id": "42"},
        {"id": True},
        {"consecutive_errors": True},
        {"check_interval_minutes": True},
        {"default_interval_minutes": True},
        {"name": None},
        {"currency": b"EUR"},
        {"threshold_value": 10},
        {"current": Decimal("-0")},
        {"current": Decimal("1e15")},
        {"last_checked_at": date(2026, 3, 1)},
    ],
)
def test_rejects_not_well_formed_field(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"."):
        _make_view(**overrides)


def test_out_of_stock_defaults_to_false() -> None:
    assert _make_view().out_of_stock is False
    assert _make_view(out_of_stock=True).out_of_stock is True


@pytest.mark.parametrize("value", ["yes", 1, 0, None])
def test_out_of_stock_must_be_a_bool(value: object) -> None:
    with pytest.raises(ValueError, match=r"^out_of_stock:"):
        _make_view(out_of_stock=value)


def test_rejection_names_the_field() -> None:
    with pytest.raises(ValueError, match=r"^id:"):
        _make_view(id=0)
    with pytest.raises(ValueError, match=r"^currency:"):
        _make_view(currency="eur")
    with pytest.raises(ValueError, match=r"^last_checked_at:"):
        _make_view(last_checked_at=datetime(2026, 1, 1))


def test_frozen() -> None:
    view = _make_view()
    with pytest.raises(FrozenInstanceError):
        view.id = 99  # type: ignore[misc]
