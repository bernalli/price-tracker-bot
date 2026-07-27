"""Outlier detection for price history (median-ratio rejection)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
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

# Dedicated low-side threshold: a price below median / LOW_OUTLIER_RATIO is
# flagged (parse-error guard, e.g. 100x/1000x scale mistakes). Deliberately
# independent from `max_ratio` so steep-but-legitimate discounts (clearance,
# Black Friday) are never rejected by the high-side tolerance.
LOW_OUTLIER_RATIO = Decimal("50")

# A read this far above the median is not a price change, it is garbage: reject
# outright, never confirmable. Sits far above `DEFAULT_MAX_RATIO` so a genuine
# (if dramatic) repricing is only held for confirmation, not discarded.
ABSURD_HIGH_RATIO = Decimal("20")

# A read below this fraction of the median is too steep to trust from a single
# sample. Not absurd — clearance and Black Friday really do halve prices — so it
# is held until a second, agreeing read confirms it rather than rejected.
SUSPICIOUS_DROP_RATIO = Decimal("0.6")

# Consecutive agreeing reads required before an implausible price is accepted.
REQUIRED_CONFIRMATIONS = 2

# Two held reads count as agreeing when they are within this relative distance.
CONFIRMATION_TOLERANCE = Decimal("0.02")


class ReadVerdict(StrEnum):
    """What the pipeline should do with a freshly scraped price."""

    ACCEPT = "accept"
    """Plausible against recent history — persist and let it alert."""

    CONFIRM = "confirm"
    """Implausible but not impossible — hold until a second read agrees."""

    REJECT = "reject"
    """Physically implausible (non-positive, or a scale-error magnitude)."""


def classify_read(
    price: Decimal,
    history: list[Decimal],
    *,
    max_ratio: Decimal = DEFAULT_MAX_RATIO,
    suspicious_drop_ratio: Decimal = SUSPICIOUS_DROP_RATIO,
    absurd_ratio: Decimal = ABSURD_HIGH_RATIO,
) -> ReadVerdict:
    """Grade a scraped price against recent history.

    Three bands, because a single scrape is not evidence:

    * ``REJECT`` — non-positive, or off by a scale-error magnitude. Garbage.
    * ``CONFIRM`` — above ``max_ratio`` × median, or below
      ``suspicious_drop_ratio`` × median. Might be real, might be a page that
      rendered somebody else's price; a second agreeing read decides.
    * ``ACCEPT`` — consistent with history.

    Short histories (< ``MIN_HISTORY``) cannot support any judgement, so every
    read is accepted: a brand-new product must be allowed to establish a
    baseline.
    """
    if price <= 0:
        return ReadVerdict.REJECT
    if len(history) < MIN_HISTORY:
        return ReadVerdict.ACCEPT

    med = Decimal(str(median(history)))
    if med == 0:
        return ReadVerdict.ACCEPT

    if price > med * absurd_ratio or med / price > LOW_OUTLIER_RATIO:
        return ReadVerdict.REJECT
    if price > med * max_ratio or price < med * suspicious_drop_ratio:
        return ReadVerdict.CONFIRM
    return ReadVerdict.ACCEPT


def reads_agree(
    first: Decimal, second: Decimal, *, tolerance: Decimal = CONFIRMATION_TOLERANCE
) -> bool:
    """Return True when two held reads are close enough to confirm each other.

    Exact equality would be too strict: a site under load may serve 187.95 and
    then 187.90 for the same offer, and both are the same claim about reality.
    """
    if first <= 0 or second <= 0:
        return False
    return abs(first - second) / max(first, second) <= tolerance


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
    - A price < median / LOW_OUTLIER_RATIO is also an outlier (parse-error
      guard: 100x/1000x scale mistakes). The low side ignores `max_ratio` so
      legitimate steep discounts are not rejected.

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
    is_low_outlier = inv_ratio > LOW_OUTLIER_RATIO

    return OutlierResult(
        is_outlier=is_high_outlier or is_low_outlier,
        median=med,
        ratio=ratio,
        history_n=len(history),
    )
