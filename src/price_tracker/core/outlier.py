"""Outlier detection for price history (median-ratio rejection)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from price_tracker.observability.metrics import MetricsRegistry


@dataclass(frozen=True)
class OutlierResult:
    """Result of outlier check."""

    is_outlier: bool
    median: Decimal | None = None
    ratio: Decimal | None = None
    history_n: int = 0


# Minimum history points to enable detection (avoid false positives on short series)
MIN_HISTORY = 5

# Default tolerance: a price more than `max_ratio` × median is flagged
DEFAULT_MAX_RATIO = Decimal("2.5")


def is_outlier(
    price: Decimal,
    history: list[Decimal],
    *,
    max_ratio: Decimal = DEFAULT_MAX_RATIO,
    metrics: MetricsRegistry | None = None,
    scraper: str = "unknown",
    domain: str = "unknown",
) -> OutlierResult:
    """Return whether `price` is anomalously high vs `history`.

    Logic:
    - Zero or negative prices are always outliers.
    - Histories shorter than MIN_HISTORY skip detection (return False).
    - A price > max_ratio × median(history) is an outlier.
    - A price < median / max_ratio is also an outlier (sudden drop, likely
      installment scrape or wrong-variant). Symmetric ratio check.

    When `metrics` is provided and the result is an outlier, emits
    `outlier_rejected_total{scraper, domain}` once.
    """
    result = _compute(price, history, max_ratio=max_ratio)
    if result.is_outlier and metrics is not None:
        metrics.outlier_rejected_total.labels(scraper=scraper, domain=domain).inc()
    return result


def _compute(
    price: Decimal,
    history: list[Decimal],
    *,
    max_ratio: Decimal,
) -> OutlierResult:
    if price <= 0:
        return OutlierResult(is_outlier=True, history_n=len(history))

    if len(history) < MIN_HISTORY:
        return OutlierResult(is_outlier=False, history_n=len(history))

    med = Decimal(str(median(history)))
    if med == 0:
        return OutlierResult(is_outlier=False, median=med, history_n=len(history))

    ratio = price / med
    inv_ratio = med / price

    is_high_outlier = ratio > max_ratio
    is_low_outlier = inv_ratio > max_ratio

    return OutlierResult(
        is_outlier=is_high_outlier or is_low_outlier,
        median=med,
        ratio=ratio,
        history_n=len(history),
    )
