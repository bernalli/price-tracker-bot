"""Tests for median-ratio outlier detection."""

from __future__ import annotations

from decimal import Decimal

import pytest
from prometheus_client import CollectorRegistry

from price_tracker.core.outlier import OutlierResult, is_outlier
from price_tracker.observability.metrics import MetricsRegistry


def _hist(values: list[float]) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


def test_no_history_returns_not_outlier():
    result = is_outlier(Decimal("100"), [])
    assert isinstance(result, OutlierResult)
    assert result.is_outlier is False


def test_short_history_returns_not_outlier():
    result = is_outlier(Decimal("100"), _hist([100, 100, 100]))  # < 5 → no detection
    assert result.is_outlier is False


def test_normal_price_within_2x_median_not_outlier():
    history = _hist([100, 102, 98, 105, 100, 99, 101, 103, 100, 100])
    result = is_outlier(Decimal("110"), history)
    assert result.is_outlier is False


def test_3x_median_is_outlier():
    history = _hist([100, 102, 98, 105, 100, 99, 101, 103, 100, 100])
    result = is_outlier(Decimal("310"), history)
    assert result.is_outlier is True
    assert result.median == Decimal("100")
    assert result.ratio is not None
    assert result.ratio >= Decimal("3.0")


def test_outlier_threshold_configurable():
    history = _hist([100] * 10)
    # ratio 2.5 with default threshold 2.0
    result = is_outlier(Decimal("250"), history, max_ratio=Decimal("2.0"))
    assert result.is_outlier is True
    result_lenient = is_outlier(Decimal("250"), history, max_ratio=Decimal("3.0"))
    assert result_lenient.is_outlier is False


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (Decimal("0"), True),  # zero price = always outlier
        (Decimal("-5"), True),  # negative = always outlier
    ],
)
def test_invalid_price_is_outlier(price, expected):
    history = _hist([100] * 10)
    assert is_outlier(price, history).is_outlier is expected


def test_zero_median_history_returns_not_outlier():
    """If median of history is 0, the ratio is undefined → return not-outlier."""
    history = _hist([0, 0, 0, 0, 0, 0])
    result = is_outlier(Decimal("100"), history)
    assert result.is_outlier is False
    assert result.median == Decimal("0")


@pytest.mark.parametrize(
    "price",
    [
        Decimal("30"),  # -70% (clearance)
        Decimal("10"),  # -90% (Black Friday doorbuster)
    ],
)
def test_steep_legitimate_drop_is_not_outlier(price):
    """Bug #18: steep but legitimate discounts must pass the low-side check.

    The low branch used max_ratio (2.5), classifying ANY price below
    median/2.5 (~ -60%) as an outlier — clearance/Black Friday drops were
    never persisted nor alerted. The low threshold is now the dedicated
    LOW_OUTLIER_RATIO (50), so only ~ -98%+ reads are rejected.
    """
    history = _hist([100, 102, 98, 105, 100, 99, 101])
    result = is_outlier(price, history)
    assert result.is_outlier is False


def test_parse_error_scale_drop_still_outlier():
    """A price ~1000x below the median (e.g. 0.099 on a ~100 history) is
    almost certainly a 100x/1000x parse error and must still be rejected."""
    history = _hist([100, 102, 98, 105, 100, 99, 101])
    result = is_outlier(Decimal("0.099"), history)
    assert result.is_outlier is True
    assert result.median == Decimal("100")


def test_low_outlier_when_price_implausibly_below_median():
    """A price below median / LOW_OUTLIER_RATIO is an outlier (parse-error
    guard). The low branch deliberately ignores ``max_ratio``: tightening the
    high-side tolerance must not start rejecting legitimate steep discounts.
    """
    history = _hist([100, 100, 100, 100, 100, 100, 100])
    result = is_outlier(Decimal("1"), history, max_ratio=Decimal("2.5"))
    # 100/1 = 100 > 50 → low outlier
    assert result.is_outlier is True
    assert result.median == Decimal("100")


def test_outlier_detector_emits_metric_when_rejecting():
    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    history = [Decimal("100")] * 10
    result = is_outlier(
        Decimal("1"),
        history,
        metrics=metrics,
        scraper="amazon",
        domain="amazon.com",
    )
    assert result.is_outlier is True
    val = reg.get_sample_value(
        "price_tracker_outlier_rejected_total",
        {"scraper": "amazon", "domain": "amazon.com"},
    )
    assert val == 1


def test_outlier_detector_does_not_emit_when_not_rejecting():
    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    history = [Decimal("100")] * 10
    result = is_outlier(
        Decimal("105"),
        history,
        metrics=metrics,
        scraper="amazon",
        domain="amazon.com",
    )
    assert result.is_outlier is False
    val = reg.get_sample_value(
        "price_tracker_outlier_rejected_total",
        {"scraper": "amazon", "domain": "amazon.com"},
    )
    assert val is None
