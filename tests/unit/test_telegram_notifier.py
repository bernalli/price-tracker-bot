"""Tests for TelegramNotifier metrics instrumentation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from prometheus_client import CollectorRegistry

from price_tracker.notifier.telegram import TelegramNotifier
from price_tracker.observability.metrics import MetricsRegistry


@pytest.mark.asyncio
async def test_notifier_emits_sent_metric_on_success() -> None:
    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    notifier = TelegramNotifier(bot, metrics=metrics)

    await notifier(123, "x")

    val = reg.get_sample_value(
        "price_tracker_notification_sent_total",
        {"type": "immediate", "channel": "telegram"},
    )
    assert val == 1


@pytest.mark.asyncio
async def test_notifier_does_not_emit_sent_metric_on_failure() -> None:
    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))
    notifier = TelegramNotifier(bot, metrics=metrics)

    await notifier(123, "x")  # exception swallowed by notifier

    val = reg.get_sample_value(
        "price_tracker_notification_sent_total",
        {"type": "immediate", "channel": "telegram"},
    )
    assert val is None


@pytest.mark.asyncio
async def test_notifier_works_without_metrics() -> None:
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    notifier = TelegramNotifier(bot)

    await notifier(456, "hello")

    bot.send_message.assert_awaited_once()
