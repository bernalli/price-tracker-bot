"""Tests for alert formatting and threshold trigger logic."""

from __future__ import annotations

from decimal import Decimal

import pytest

from price_tracker.core.alert import (
    PriceAlert,
    crosses_threshold,
    format_alert,
    format_error_notification,
)


def test_crosses_threshold_percentage_drop():
    assert crosses_threshold(
        old=Decimal("100"),
        new=Decimal("89"),
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    ) is True


def test_crosses_threshold_percentage_no_trigger():
    assert crosses_threshold(
        old=Decimal("100"),
        new=Decimal("95"),
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    ) is False


def test_crosses_threshold_absolute_drop():
    assert crosses_threshold(
        old=Decimal("100"),
        new=Decimal("89"),
        threshold_type="absolute",
        threshold_value=Decimal("10"),
    ) is True


def test_crosses_threshold_target_price():
    assert crosses_threshold(
        old=Decimal("100"),
        new=Decimal("89"),
        threshold_type="target",
        threshold_value=Decimal("90"),
    ) is True


def test_crosses_threshold_target_price_above():
    assert crosses_threshold(
        old=Decimal("100"),
        new=Decimal("91"),
        threshold_type="target",
        threshold_value=Decimal("90"),
    ) is False


def test_crosses_threshold_no_drop():
    assert crosses_threshold(
        old=Decimal("100"),
        new=Decimal("110"),
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    ) is False


def test_format_alert_includes_drop_percentage():
    alert = PriceAlert(
        product_id=1,
        product_name="Test Widget",
        url="https://example.com/p/1",
        old_price=Decimal("100"),
        new_price=Decimal("80"),
        currency="EUR",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )
    text = format_alert(alert)
    assert "Test Widget" in text
    assert "100" in text
    assert "80" in text
    assert "20" in text or "20%" in text
    assert "EUR" in text or "€" in text


def test_format_alert_escapes_html():
    alert = PriceAlert(
        product_id=1,
        product_name="<script>",
        url="https://example.com/",
        old_price=Decimal("100"),
        new_price=Decimal("80"),
        currency="EUR",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )
    text = format_alert(alert)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_format_error_notification_mentions_count():
    text = format_error_notification(
        product={"id": 1, "name": "Widget", "url": "https://x"},
        error_count=10,
        max_errors=10,
    )
    assert "Widget" in text
    assert "10" in text
