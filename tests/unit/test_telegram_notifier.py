"""Tests for TelegramNotifier metrics instrumentation."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from prometheus_client import CollectorRegistry
from telegram import InlineKeyboardMarkup

from price_tracker.core.textlimits import SAFE_LIMIT, visible_length
from price_tracker.db.models import NotificationPrefs
from price_tracker.notifier.preferences import EffectivePrefs, ThrottleWindow
from price_tracker.notifier.telegram import TelegramNotifier
from price_tracker.observability.metrics import MetricsRegistry


def _prefs(**overrides: object) -> EffectivePrefs:
    values: dict[str, object] = {
        "mute": False,
        "mute_until": None,
        "digest_mode": False,
        "digest_interval_minutes": 60,
        "quiet_hours_start": None,
        "quiet_hours_end": None,
        "throttle_per_hour": None,
        "timezone": "UTC",
    }
    values.update(overrides)
    return EffectivePrefs(**values)  # type: ignore[arg-type]


def _buttons() -> list[list[dict[str, str]]]:
    return [
        [{"text": "Reactivate", "callback_data": "ops_react_10"}],
        [{"text": "Delete", "callback_data": "ops_del_10"}],
    ]


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
async def test_dedupe_does_not_swallow_a_retry_of_a_dropped_alert() -> None:
    """An alert that was dropped must not be marked as already handled.

    The in-process dedupe set records the event id on arrival, so a first
    attempt suppressed by a preference poisoned every retry: the retry
    short-circuits on the dedupe hit and reports success for a message that was
    never delivered by anyone.
    """
    from unittest.mock import AsyncMock as _AsyncMock

    from price_tracker.db.models import NotificationPrefs

    bot = AsyncMock()
    prefs = _AsyncMock()
    prefs.resolve = _AsyncMock(
        return_value=NotificationPrefs(user_id=1, product_id=None, mute=True)
    )
    notifier = TelegramNotifier(bot, prefs=prefs)
    alert = {"event_id": "evt-1", "text": "drop"}

    first = await notifier.notify_alert(user_id=1, product_id=7, alert=alert)
    second = await notifier.notify_alert(user_id=1, product_id=7, alert=alert)

    assert first is False
    assert second is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notifier_works_without_metrics() -> None:
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    notifier = TelegramNotifier(bot)

    await notifier(456, "hello")

    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_operational_routes_without_product_id() -> None:
    bot = AsyncMock()
    prefs = AsyncMock()
    prefs.resolve_global = AsyncMock(return_value=_prefs())
    notifier = TelegramNotifier(bot, prefs=prefs)

    delivered = await notifier(
        1,
        "Operational notice",
        payload={"kind": "operational", "event_id": "ops-1"},
    )

    assert delivered is True
    prefs.resolve_global.assert_awaited_once_with(user_id=1)
    prefs.resolve.assert_not_awaited()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_operational_bypasses_mute() -> None:
    bot = AsyncMock()
    prefs = AsyncMock()
    prefs.resolve_global = AsyncMock(return_value=_prefs(mute=True))
    notifier = TelegramNotifier(bot, prefs=prefs)

    assert await notifier(1, "Operational", payload={"kind": "operational"}) is True
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_price_alert_still_muted() -> None:
    bot = AsyncMock()
    prefs = AsyncMock()
    prefs.resolve = AsyncMock(return_value=_prefs(mute=True))
    notifier = TelegramNotifier(bot, prefs=prefs)

    assert (await notifier(1, "Price", product_id=10, payload={"kind": "price"})) is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_operational_in_quiet_hours_without_digest_is_enqueued_not_dropped() -> None:
    bot = AsyncMock()
    digest = AsyncMock()
    prefs = AsyncMock()
    prefs.resolve_global = AsyncMock(
        return_value=_prefs(quiet_hours_start="00:00", quiet_hours_end="23:59")
    )
    notifier = TelegramNotifier(bot, prefs=prefs, digest=digest)

    assert await notifier(1, "Operational", payload={"kind": "operational"}) is True
    digest.enqueue.assert_awaited_once_with(
        user_id=1,
        product_id=None,
        payload={"kind": "operational", "text": "Operational"},
    )
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_operational_throttled_without_digest_is_enqueued() -> None:
    now = datetime.now(UTC)
    bot = AsyncMock()
    digest = AsyncMock()
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(
        return_value=NotificationPrefs(
            user_id=1,
            throttle_state_json=ThrottleWindow(timestamps=[now.timestamp()]).to_json(),
        )
    )
    prefs = AsyncMock()
    prefs._repo = repo
    prefs.resolve_global = AsyncMock(return_value=_prefs(throttle_per_hour=1))
    notifier = TelegramNotifier(bot, prefs=prefs, digest=digest)

    assert await notifier(1, "Operational", payload={"kind": "operational"}) is True
    digest.enqueue.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_operational_not_throttled_records_window() -> None:
    bot = AsyncMock()
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    prefs = AsyncMock()
    prefs._repo = repo
    prefs.resolve_global = AsyncMock(return_value=_prefs(throttle_per_hour=2))
    notifier = TelegramNotifier(bot, prefs=prefs)

    assert await notifier(1, "Operational", payload={"kind": "operational"}) is True
    repo.upsert_notification_prefs.assert_awaited_once()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_buttons_become_inline_keyboard_for_direct_send() -> None:
    bot = AsyncMock()
    notifier = TelegramNotifier(bot)

    assert await notifier(1, "Operational", payload={"buttons": _buttons()}) is True

    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert len(markup.inline_keyboard) == 2


@pytest.mark.asyncio
async def test_buttons_become_inline_keyboard_for_preference_send() -> None:
    bot = AsyncMock()
    prefs = AsyncMock()
    prefs.resolve_global = AsyncMock(return_value=_prefs())
    notifier = TelegramNotifier(bot, prefs=prefs)

    assert await notifier(1, "Operational", payload={"kind": "operational", "buttons": _buttons()})

    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert len(markup.inline_keyboard) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "buttons",
    [
        "not-a-list",
        ["not-a-row"],
        [["not-a-button"]],
        [[{"text": 1, "callback_data": "ops_1"}]],
        [[{"text": "Button", "callback_data": 1}]],
        [[{"text": "Button", "callback_data": "x" * 65}]],
    ],
)
async def test_malformed_buttons_send_without_keyboard(buttons: object) -> None:
    bot = AsyncMock()
    notifier = TelegramNotifier(bot)

    assert await notifier(1, "Operational", payload={"buttons": buttons}) is True
    assert bot.send_message.await_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_long_text_is_sent_in_chunks_with_keyboard_on_last() -> None:
    reg = CollectorRegistry()
    bot = AsyncMock()
    notifier = TelegramNotifier(bot, metrics=MetricsRegistry(registry=reg))
    text = "\n".join(f"<b>{index}</b> {'x' * 30}" for index in range(250))

    assert await notifier(1, text, payload={"buttons": _buttons()}) is True

    calls = bot.send_message.await_args_list
    assert len(calls) >= 2
    assert all(visible_length(call.kwargs["text"]) <= SAFE_LIMIT for call in calls)
    assert all(call.kwargs["reply_markup"] is None for call in calls[:-1])
    assert isinstance(calls[-1].kwargs["reply_markup"], InlineKeyboardMarkup)
    assert (
        reg.get_sample_value(
            "price_tracker_notification_sent_total",
            {"type": "immediate", "channel": "telegram"},
        )
        == 1
    )


@pytest.mark.asyncio
async def test_chunk_failure_returns_false_and_logs_index(caplog: pytest.LogCaptureFixture) -> None:
    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=[None, RuntimeError("boom")])
    notifier = TelegramNotifier(bot)
    text = "\n".join(f"<b>{index}</b> {'x' * 30}" for index in range(250))

    with caplog.at_level("WARNING"):
        assert await notifier(1, text) is False

    assert "chunk 2" in caplog.text


@pytest.mark.asyncio
async def test_event_id_dedupe_still_applies_to_operational() -> None:
    bot = AsyncMock()
    prefs = AsyncMock()
    prefs.resolve_global = AsyncMock(return_value=_prefs())
    notifier = TelegramNotifier(bot, prefs=prefs)
    payload = {"kind": "operational", "event_id": "ops-duplicate"}

    assert await notifier(1, "Operational", payload=payload) is True
    assert await notifier(1, "Operational", payload=payload) is True
    bot.send_message.assert_awaited_once()
