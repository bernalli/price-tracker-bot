"""Integration test for notify_alert wiring: prefs + digest + dedupe.

Covers the alert dispatch flow in TelegramNotifier (item 28, F3.D):
- immediate send when no prefs
- mute drops alert
- quiet hours + digest mode -> enqueue
- quiet hours + no digest -> drop
- duplicate event_id -> deduped
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from prometheus_client import CollectorRegistry

from price_tracker.db.models import NotificationPrefs
from price_tracker.notifier.digest import DigestService
from price_tracker.notifier.preferences import PreferencesManager
from price_tracker.notifier.telegram import TelegramNotifier
from price_tracker.observability.metrics import MetricsRegistry


@pytest.fixture
def repo_mock() -> AsyncMock:
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.enqueue_digest = AsyncMock(return_value=1)
    repo.list_pending_digest = AsyncMock(return_value=[])
    repo.mark_digest_flushed = AsyncMock()
    return repo


@pytest.mark.asyncio
async def test_immediate_send_when_no_prefs(repo_mock: AsyncMock) -> None:
    bot = AsyncMock()
    metrics = MetricsRegistry(registry=CollectorRegistry())
    prefs_mgr = PreferencesManager(repo=repo_mock)
    digest = DigestService(repo=repo_mock, bot=bot, metrics=metrics)
    notifier = TelegramNotifier(bot=bot, metrics=metrics, prefs=prefs_mgr, digest=digest)
    await notifier.notify_alert(
        user_id=42, product_id=10, alert={"product_name": "X", "domain": "amazon.com"}
    )
    bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_mute_drops_alert(repo_mock: AsyncMock) -> None:
    repo_mock.get_notification_prefs = AsyncMock(
        side_effect=[
            None,
            NotificationPrefs(user_id=42, product_id=None, mute=True),
        ]
    )
    bot = AsyncMock()
    metrics = MetricsRegistry(registry=CollectorRegistry())
    notifier = TelegramNotifier(
        bot=bot,
        metrics=metrics,
        prefs=PreferencesManager(repo=repo_mock),
        digest=DigestService(repo=repo_mock, bot=bot, metrics=metrics),
    )
    await notifier.notify_alert(user_id=42, product_id=10, alert={"product_name": "X"})
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_quiet_hours_with_digest_enqueues(repo_mock: AsyncMock) -> None:
    repo_mock.get_notification_prefs = AsyncMock(
        side_effect=[
            None,
            NotificationPrefs(
                user_id=42,
                product_id=None,
                digest_mode=True,
                quiet_hours_start="22:00",
                quiet_hours_end="08:00",
                timezone="UTC",
            ),
        ]
    )
    bot = AsyncMock()
    metrics = MetricsRegistry(registry=CollectorRegistry())
    notifier = TelegramNotifier(
        bot=bot,
        metrics=metrics,
        prefs=PreferencesManager(repo=repo_mock),
        digest=DigestService(repo=repo_mock, bot=bot, metrics=metrics),
    )
    with freeze_time("2026-05-09 23:30:00"):
        await notifier.notify_alert(user_id=42, product_id=10, alert={"product_name": "X"})
    repo_mock.enqueue_digest.assert_awaited()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_quiet_hours_without_digest_drops(repo_mock: AsyncMock) -> None:
    repo_mock.get_notification_prefs = AsyncMock(
        side_effect=[
            None,
            NotificationPrefs(
                user_id=42,
                product_id=None,
                digest_mode=False,
                quiet_hours_start="22:00",
                quiet_hours_end="08:00",
                timezone="UTC",
            ),
        ]
    )
    bot = AsyncMock()
    metrics = MetricsRegistry(registry=CollectorRegistry())
    notifier = TelegramNotifier(
        bot=bot,
        metrics=metrics,
        prefs=PreferencesManager(repo=repo_mock),
        digest=DigestService(repo=repo_mock, bot=bot, metrics=metrics),
    )
    with freeze_time("2026-05-09 23:30:00"):
        await notifier.notify_alert(user_id=42, product_id=10, alert={"product_name": "X"})
    repo_mock.enqueue_digest.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_no_duplicate_notification_for_same_event(repo_mock: AsyncMock) -> None:
    """KPI v0.1.0: same alert event must not trigger two messages."""
    bot = AsyncMock()
    metrics = MetricsRegistry(registry=CollectorRegistry())
    notifier = TelegramNotifier(
        bot=bot,
        metrics=metrics,
        prefs=PreferencesManager(repo=repo_mock),
        digest=DigestService(repo=repo_mock, bot=bot, metrics=metrics),
    )
    alert = {"product_name": "X", "event_id": "evt-1"}
    await notifier.notify_alert(user_id=42, product_id=10, alert=alert)
    await notifier.notify_alert(user_id=42, product_id=10, alert=alert)  # replay same event
    assert bot.send_message.await_count == 1  # second is deduped
