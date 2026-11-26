"""Tests for TelegramNotifier metrics instrumentation."""

from __future__ import annotations

import asyncio
import dataclasses
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
    repo.set_throttle_state.assert_awaited_once()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_buttons_become_inline_keyboard_for_direct_send() -> None:
    bot = AsyncMock()
    notifier = TelegramNotifier(bot)

    assert await notifier(1, "Operational", payload={"buttons": _buttons()}) is True

    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert len(markup.inline_keyboard) == 2
    assert [row[0].callback_data for row in markup.inline_keyboard] == ["o:10:ra", "o:10:rm"]


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


@pytest.mark.asyncio
async def test_digest_mode_alert_does_not_consume_throttle_quota() -> None:
    """Queued digest alerts send no immediate message and must not burn throttle slots."""
    bot = AsyncMock()
    digest = AsyncMock()
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    prefs = AsyncMock()
    prefs._repo = repo
    prefs.resolve = AsyncMock(return_value=_prefs(digest_mode=True, throttle_per_hour=1))
    notifier = TelegramNotifier(bot, prefs=prefs, digest=digest)

    delivered = await notifier.notify_alert(
        user_id=1,
        product_id=10,
        alert={"kind": "price", "text": "drop", "event_id": "evt-digest-1"},
    )

    assert delivered is True
    digest.enqueue.assert_awaited_once()
    repo.set_throttle_state.assert_not_awaited()
    repo.upsert_notification_prefs.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_immediate_send_does_not_consume_throttle_quota() -> None:
    """A failed Telegram send must not consume a slot from the hourly throttle window."""
    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.set_throttle_state = AsyncMock()
    repo.upsert_notification_prefs = AsyncMock()
    prefs = AsyncMock()
    prefs._repo = repo
    prefs.resolve = AsyncMock(return_value=_prefs(throttle_per_hour=1))
    notifier = TelegramNotifier(bot, prefs=prefs)

    with pytest.raises(RuntimeError, match="telegram down"):
        await notifier.notify_alert(
            user_id=1,
            product_id=10,
            alert={"kind": "price", "text": "drop", "event_id": "evt-fail-1"},
        )

    repo.set_throttle_state.assert_not_awaited()
    repo.upsert_notification_prefs.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_alert_message_escapes_html_characters() -> None:
    """Untrusted product names and domains in dict-only alerts must be HTML-escaped."""
    bot = AsyncMock()
    notifier = TelegramNotifier(bot)

    delivered = await notifier.notify_alert(
        user_id=1,
        product_id=10,
        alert={
            "product_name": "A & B <Pro>",
            "old_price": "20",
            "new_price": "10",
            "currency": "EUR",
            "domain": "a<b>.example",
        },
    )

    assert delivered is True
    sent = bot.send_message.await_args.kwargs["text"]
    assert "A &amp; B &lt;Pro&gt;" in sent
    assert "a&lt;b&gt;.example" in sent
    assert "<Pro>" not in sent


@pytest.mark.asyncio
async def test_dedupe_seen_is_bounded() -> None:
    """The in-process dedupe cache must evict oldest entries rather than grow unbounded."""
    from price_tracker.notifier.telegram import MAX_DEDUPE_SEEN  # noqa: PLC0415

    bot = AsyncMock()
    notifier = TelegramNotifier(bot)
    for i in range(MAX_DEDUPE_SEEN + 25):
        notifier._mark_delivered(f"evt-{i}")

    assert len(notifier._dedupe_seen) == MAX_DEDUPE_SEEN
    assert f"evt-{MAX_DEDUPE_SEEN + 24}" in notifier._dedupe_seen
    assert "evt-0" not in notifier._dedupe_seen


@pytest.mark.asyncio
async def test_post_send_throttle_preserves_concurrent_prefs_and_dedupe_on_db_error() -> None:
    """Throttle and preference writes must not clobber each other in SQLite."""
    import aiosqlite  # noqa: PLC0415

    from price_tracker.db.migrator import apply_migrations  # noqa: PLC0415
    from price_tracker.db.repository import Repository  # noqa: PLC0415
    from price_tracker.main import MIGRATIONS_DIR  # noqa: PLC0415
    from price_tracker.notifier.preferences import PreferencesManager, update_prefs  # noqa: PLC0415

    # Case 1: Real SQLite interleaving — /mute commits AFTER notifier's SELECT and BEFORE its
    # throttle write; a subsequent field-specific preference edit also preserves throttle_state.
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await apply_migrations(conn, MIGRATIONS_DIR)
        real_repo = Repository(conn)
        await real_repo.ensure_user(1)
        await update_prefs(real_repo, 1, None, throttle_per_hour=5)

        sending = asyncio.Event()
        resume_send = asyncio.Event()

        async def _paused_send(**kwargs: object) -> None:  # noqa: ARG001
            sending.set()
            await resume_send.wait()

        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=_paused_send)
        notifier = TelegramNotifier(bot, prefs=PreferencesManager(real_repo))

        delivery = asyncio.create_task(
            notifier.notify_alert(
                user_id=1, product_id=10, alert={"text": "drop", "event_id": "evt-race-1"}
            )
        )
        try:
            await asyncio.wait_for(sending.wait(), timeout=2)
            # Commit /mute while delivery is in flight (between SELECT and set_throttle_state).
            await update_prefs(real_repo, 1, None, mute=True)
            resume_send.set()
            assert await asyncio.wait_for(delivery, timeout=2) is True
        finally:
            resume_send.set()
            if not delivery.done():
                delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)

        # Follow with an unrelated preference update (must not clobber mute or throttle_state).
        await update_prefs(real_repo, 1, None, timezone="UTC")

        final = await real_repo.get_notification_prefs(user_id=1, product_id=None)
        assert final is not None
        assert final.mute is True
        assert final.timezone == "UTC"
        assert final.throttle_per_hour == 5
        assert final.throttle_state_json is not None
        assert len(ThrottleWindow.from_json(final.throttle_state_json).timestamps) == 1
    finally:
        await conn.close()

    # Case 2: DB failure when persisting throttle after delivery still marks dedupe & returns True.
    initial = NotificationPrefs(user_id=1, product_id=None, mute=False, throttle_per_hour=5)
    bot2 = AsyncMock()
    repo2 = AsyncMock()
    repo2.get_notification_prefs = AsyncMock(return_value=initial)
    repo2.set_throttle_state = AsyncMock(side_effect=RuntimeError("db locked"))
    prefs2 = AsyncMock()
    prefs2._repo = repo2
    prefs2.resolve = AsyncMock(return_value=_prefs(throttle_per_hour=5))
    notifier2 = TelegramNotifier(bot2, prefs=prefs2)

    alert = {"text": "drop", "event_id": "evt-db-err-1"}
    assert await notifier2.notify_alert(user_id=1, product_id=10, alert=alert) is True
    assert await notifier2.notify_alert(user_id=1, product_id=10, alert=alert) is True
    bot2.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_alerts_share_the_last_throttle_slot() -> None:
    """Concurrent alerts for the same user must serialize so limit=1 sends at most once."""
    row = NotificationPrefs(user_id=1, throttle_per_hour=1)

    async def get_prefs(**kwargs: object) -> NotificationPrefs:  # noqa: ARG001
        return row

    async def save_throttle(*, user_id: int, throttle_state_json: str) -> None:  # noqa: ARG001
        nonlocal row
        row = dataclasses.replace(row, throttle_state_json=throttle_state_json)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(**kwargs: object) -> None:  # noqa: ARG001
        entered.set()
        await release.wait()

    repo = AsyncMock()
    repo.get_notification_prefs.side_effect = get_prefs
    repo.set_throttle_state.side_effect = save_throttle
    prefs = AsyncMock()
    prefs._repo = repo
    prefs.resolve.return_value = _prefs(throttle_per_hour=1)
    bot = AsyncMock()
    bot.send_message.side_effect = send
    notifier = TelegramNotifier(bot, prefs=prefs)

    first = asyncio.create_task(
        notifier.notify_alert(user_id=1, product_id=10, alert={"text": "A"})
    )
    second = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(
            notifier.notify_alert(user_id=1, product_id=11, alert={"text": "B"})
        )
        await asyncio.sleep(0)
        release.set()
        assert list(await asyncio.gather(first, second)) == [True, False]
        bot.send_message.assert_awaited_once()
        assert row.throttle_state_json is not None
        assert len(ThrottleWindow.from_json(row.throttle_state_json).timestamps) == 1
    finally:
        release.set()
        tasks = [first] if second is None else [first, second]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
