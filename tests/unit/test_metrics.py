import pytest
from prometheus_client import CollectorRegistry

from price_tracker.observability.metrics import MetricsRegistry


@pytest.fixture
def fresh_registry() -> CollectorRegistry:
    return CollectorRegistry()


class TestMetricsRegistry:
    def test_price_check_total_counter(self, fresh_registry):
        m = MetricsRegistry(registry=fresh_registry)
        m.price_check_total.labels(scraper="amazon", domain="amazon.com", status="success").inc()
        m.price_check_total.labels(scraper="amazon", domain="amazon.com", status="success").inc(2)
        val = fresh_registry.get_sample_value(
            "price_tracker_price_check_total",
            {"scraper": "amazon", "domain": "amazon.com", "status": "success"},
        )
        assert val == 3

    def test_scraper_duration_histogram(self, fresh_registry):
        m = MetricsRegistry(registry=fresh_registry)
        m.scraper_duration_seconds.labels(scraper="amazon", domain="amazon.com").observe(0.42)
        m.scraper_duration_seconds.labels(scraper="amazon", domain="amazon.com").observe(1.5)
        count = fresh_registry.get_sample_value(
            "price_tracker_scraper_duration_seconds_count",
            {"scraper": "amazon", "domain": "amazon.com"},
        )
        assert count == 2

    def test_quarantine_state_gauge(self, fresh_registry):
        m = MetricsRegistry(registry=fresh_registry)
        m.quarantine_state.labels(domain="xteink.com", state="LOCKED_T3").set(3)
        val = fresh_registry.get_sample_value(
            "price_tracker_quarantine_state",
            {"domain": "xteink.com", "state": "LOCKED_T3"},
        )
        assert val == 3

    def test_no_user_id_label_present(self, fresh_registry):
        """Privacy invariant: no metric must accept user_id label."""
        m = MetricsRegistry(registry=fresh_registry)
        for metric_name in [
            "price_check_total",
            "notification_sent_total",
            "notification_skipped_total",
            "outlier_rejected_total",
        ]:
            metric = getattr(m, metric_name)
            assert "user_id" not in metric._labelnames

    def test_notification_sent_total(self, fresh_registry):
        m = MetricsRegistry(registry=fresh_registry)
        m.notification_sent_total.labels(type="immediate", channel="telegram").inc()
        val = fresh_registry.get_sample_value(
            "price_tracker_notification_sent_total",
            {"type": "immediate", "channel": "telegram"},
        )
        assert val == 1

    def test_uptime_gauge_callable(self, fresh_registry):
        m = MetricsRegistry(registry=fresh_registry)
        m.bot_uptime_seconds.set(120.0)
        val = fresh_registry.get_sample_value("price_tracker_bot_uptime_seconds")
        assert val == 120.0
