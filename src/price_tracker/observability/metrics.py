"""Prometheus metrics registry.

All metrics are namespaced under "price_tracker_". No metric carries
user_id as label (privacy + cardinality invariant).
"""

from __future__ import annotations

from prometheus_client import REGISTRY as DEFAULT_REGISTRY
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)

NAMESPACE = "price_tracker"

_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


class MetricsRegistry:
    """Container for all metrics; pass a CollectorRegistry for test isolation."""

    def __init__(self, *, registry: CollectorRegistry | None = None) -> None:
        reg = registry if registry is not None else DEFAULT_REGISTRY
        self._registry = reg

        self.price_check_total = Counter(
            f"{NAMESPACE}_price_check_total",
            "Total scrape attempts.",
            ["scraper", "domain", "status"],
            registry=reg,
        )
        self.scraper_duration_seconds = Histogram(
            f"{NAMESPACE}_scraper_duration_seconds",
            "Scrape latency in seconds.",
            ["scraper", "domain"],
            buckets=_DURATION_BUCKETS,
            registry=reg,
        )
        self.outlier_rejected_total = Counter(
            f"{NAMESPACE}_outlier_rejected_total",
            "Prices rejected by median-ratio outlier detector.",
            ["scraper", "domain"],
            registry=reg,
        )
        self.notification_sent_total = Counter(
            f"{NAMESPACE}_notification_sent_total",
            "Notifications successfully delivered.",
            ["type", "channel"],
            registry=reg,
        )
        self.notification_skipped_total = Counter(
            f"{NAMESPACE}_notification_skipped_total",
            "Notifications skipped due to user preferences.",
            ["reason"],
            registry=reg,
        )
        self.quarantine_state = Gauge(
            f"{NAMESPACE}_quarantine_state",
            "Per-domain quarantine state (0=CLOSED 1=T1 2=T2 3=T3 4=HALF_OPEN).",
            ["domain", "state"],
            registry=reg,
        )
        self.quarantine_transitions_total = Counter(
            f"{NAMESPACE}_quarantine_transitions_total",
            "State machine transitions.",
            ["domain", "from_state", "to_state"],
            registry=reg,
        )
        self.quarantine_skip_total = Counter(
            f"{NAMESPACE}_quarantine_skip_total",
            "Schedule ticks where a domain was skipped due to quarantine.",
            ["domain"],
            registry=reg,
        )
        self.currency_lookups_total = Counter(
            f"{NAMESPACE}_currency_lookups_total",
            "Currency conversion lookups.",
            ["result"],
            registry=reg,
        )
        self.currency_cache_hit_rate = Gauge(
            f"{NAMESPACE}_currency_cache_hit_rate",
            "Rolling 5-min cache hit rate for currency lookups.",
            registry=reg,
        )
        self.products_tracked_total = Gauge(
            f"{NAMESPACE}_products_tracked_total",
            "Active tracked products (no per-user breakdown).",
            registry=reg,
        )
        self.bot_uptime_seconds = Gauge(
            f"{NAMESPACE}_bot_uptime_seconds",
            "Seconds since bot start.",
            registry=reg,
        )
        self.scheduler_jobs_active = Gauge(
            f"{NAMESPACE}_scheduler_jobs_active",
            "Jobs queued in the current scheduler tick.",
            registry=reg,
        )
        self.digest_queue_size = Gauge(
            f"{NAMESPACE}_digest_queue_size",
            "Pending digest entries waiting for flush.",
            registry=reg,
        )

    @property
    def registry(self) -> CollectorRegistry:
        return self._registry
