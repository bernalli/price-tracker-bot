"""Integration test for notify_alert wiring: prefs + digest + dedupe.

Covers the alert dispatch flow in TelegramNotifier (Task 28, F3.D):
- immediate send when no prefs
- mute drops alert
- quiet hours + digest mode -> enqueue
- quiet hours + no digest -> drop
- duplicate event_id -> deduped
"""

from __future__ import annotations

import json
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
async def test_digest_routed_alert_counts_skip_reason_once(repo_mock: AsyncMock) -> None:
    """Bug #64: one digest-routed alert must increment digest_pending exactly once.

    DigestService.enqueue used to bump notification_skipped_total{digest_pending}
    on top of TelegramNotifier._emit_skipped, double-counting every routed alert.
    """
    repo_mock.get_notification_prefs = AsyncMock(
        side_effect=[
            None,
            NotificationPrefs(user_id=42, product_id=None, digest_mode=True),
        ]
    )
    bot = AsyncMock()
    registry = CollectorRegistry()
    metrics = MetricsRegistry(registry=registry)
    notifier = TelegramNotifier(
        bot=bot,
        metrics=metrics,
        prefs=PreferencesManager(repo=repo_mock),
        digest=DigestService(repo=repo_mock, bot=bot, metrics=metrics),
    )
    await notifier.notify_alert(user_id=42, product_id=10, alert={"product_name": "X"})
    repo_mock.enqueue_digest.assert_awaited_once()
    val = registry.get_sample_value(
        "price_tracker_notification_skipped_total", {"reason": "digest_pending"}
    )
    assert val == 1.0


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


@pytest.mark.asyncio
async def test_operational_uses_global_prefs_not_product_override(repo_mock: AsyncMock) -> None:
    """A domain-level notice must not inherit an anchor product's quiet hours."""
    per_product = NotificationPrefs(
        user_id=42,
        product_id=10,
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
        timezone="UTC",
    )
    global_prefs = NotificationPrefs(user_id=42, product_id=None)

    async def get_prefs(*, user_id: int, product_id: int | None) -> NotificationPrefs | None:
        assert user_id == 42
        return per_product if product_id == 10 else global_prefs

    repo_mock.get_notification_prefs = AsyncMock(side_effect=get_prefs)
    bot = AsyncMock()
    metrics = MetricsRegistry(registry=CollectorRegistry())
    notifier = TelegramNotifier(
        bot=bot,
        metrics=metrics,
        prefs=PreferencesManager(repo=repo_mock),
        digest=DigestService(repo=repo_mock, bot=bot, metrics=metrics),
    )

    with freeze_time("2026-05-09 23:30:00"):
        handled = await notifier(
            42,
            "operational",
            product_id=None,
            payload={"kind": "operational", "event_id": "ops-global-now"},
        )

    assert handled is True
    bot.send_message.assert_awaited_once()
    repo_mock.enqueue_digest.assert_not_awaited()


@pytest.mark.asyncio
async def test_operational_global_quiet_hours_enqueues_without_digest_mode(
    repo_mock: AsyncMock,
) -> None:
    """Operational notices are deferred instead of silently discarded."""
    global_prefs = NotificationPrefs(
        user_id=42,
        product_id=None,
        digest_mode=False,
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
        timezone="UTC",
    )
    repo_mock.get_notification_prefs = AsyncMock(return_value=global_prefs)
    bot = AsyncMock()
    metrics = MetricsRegistry(registry=CollectorRegistry())
    notifier = TelegramNotifier(
        bot=bot,
        metrics=metrics,
        prefs=PreferencesManager(repo=repo_mock),
        digest=DigestService(repo=repo_mock, bot=bot, metrics=metrics),
    )

    with freeze_time("2026-05-09 23:30:00"):
        handled = await notifier(
            42,
            "operational",
            product_id=None,
            payload={"kind": "operational", "event_id": "ops-global-later"},
        )

    assert handled is True
    repo_mock.enqueue_digest.assert_awaited_once()
    call = repo_mock.enqueue_digest.await_args
    assert call.kwargs["user_id"] == 42
    assert call.kwargs["product_id"] is None
    assert json.loads(call.kwargs["payload"]) == {
        "kind": "operational",
        "event_id": "ops-global-later",
        "text": "operational",
    }
    bot.send_message.assert_not_awaited()
