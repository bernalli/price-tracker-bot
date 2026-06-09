"""Tests for the notification preference handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.settings import (
    _VALID_TIMEZONES,
    digest_mode_command,
    digest_now_command,
    mute_command,
    prefs_command,
    quiet_hours_command,
    throttle_command,
    timezone_command,
    unmute_command,
)


def _build_update(user_id: int, args: list[str]):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_html = AsyncMock()
    update.message.reply_text = AsyncMock()
    return update


def _build_context(args: list[str], **bot_data):
    context = MagicMock()
    context.args = args
    # @restricted now gates these handlers via db.is_user_allowed; provision an
    # allowed user by default so the handler body still runs under test.
    if "db" not in bot_data:
        db = AsyncMock()
        db.is_user_allowed = AsyncMock(return_value=True)
        db.update_user_info = AsyncMock()
        bot_data["db"] = db
    context.bot_data = bot_data
    return context


@pytest.mark.asyncio
async def test_mute_all_forever():
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["all", "forever"])
    context = _build_context(["all", "forever"], repository=repo)
    await mute_command(update, context)
    repo.upsert_notification_prefs.assert_awaited()
    args, _ = repo.upsert_notification_prefs.call_args
    prefs = args[0]
    assert prefs.user_id == 42
    assert prefs.product_id is None
    assert prefs.mute is True
    assert prefs.mute_until is None


@pytest.mark.asyncio
async def test_mute_specific_product_for_24_hours():
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["10", "24"])
    context = _build_context(["10", "24"], repository=repo)
    await mute_command(update, context)
    args, _ = repo.upsert_notification_prefs.call_args
    prefs = args[0]
    assert prefs.product_id == 10
    assert prefs.mute is True
    assert prefs.mute_until is not None


@pytest.mark.asyncio
async def test_digest_mode_on_with_interval():
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["on", "30"])
    context = _build_context(["on", "30"], repository=repo)
    await digest_mode_command(update, context)
    args, _ = repo.upsert_notification_prefs.call_args
    prefs = args[0]
    assert prefs.digest_mode is True
    assert prefs.digest_interval_minutes == 30


@pytest.mark.asyncio
async def test_quiet_hours_set():
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["22:00-08:00"])
    context = _build_context(["22:00-08:00"], repository=repo)
    await quiet_hours_command(update, context)
    args, _ = repo.upsert_notification_prefs.call_args
    prefs = args[0]
    assert prefs.quiet_hours_start == "22:00"
    assert prefs.quiet_hours_end == "08:00"


@pytest.mark.asyncio
async def test_quiet_hours_off():
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["off"])
    context = _build_context(["off"], repository=repo)
    await quiet_hours_command(update, context)
    args, _ = repo.upsert_notification_prefs.call_args
    prefs = args[0]
    assert prefs.quiet_hours_start is None
    assert prefs.quiet_hours_end is None


@pytest.mark.asyncio
async def test_timezone_invalid_rejected():
    repo = AsyncMock()
    update = _build_update(42, ["Mars/Olympus"])
    context = _build_context(["Mars/Olympus"], repository=repo)
    await timezone_command(update, context)
    update.message.reply_text.assert_awaited()
    msg = update.message.reply_text.call_args.args[0]
    assert "invalid" in msg.lower() or "unknown" in msg.lower()


@pytest.mark.asyncio
async def test_timezone_valid_persists():
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["Europe/Berlin"])
    context = _build_context(["Europe/Berlin"], repository=repo)
    await timezone_command(update, context)
    args, _ = repo.upsert_notification_prefs.call_args
    prefs = args[0]
    assert prefs.timezone == "Europe/Berlin"


@pytest.mark.asyncio
async def test_throttle_set():
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["5"])
    context = _build_context(["5"], repository=repo)
    await throttle_command(update, context)
    args, _ = repo.upsert_notification_prefs.call_args
    prefs = args[0]
    assert prefs.throttle_per_hour == 5


@pytest.mark.asyncio
async def test_prefs_renders_resolved():
    repo = AsyncMock()
    from price_tracker.db.models import NotificationPrefs

    repo.get_notification_prefs = AsyncMock(
        side_effect=[
            None,
            NotificationPrefs(
                user_id=42, product_id=None, digest_mode=True, timezone="Europe/Berlin"
            ),
        ]
    )
    update = _build_update(42, [])
    context = _build_context([], repository=repo)
    await prefs_command(update, context)
    rendered = update.message.reply_html.call_args.args[0]
    assert "digest" in rendered.lower()
    assert "Europe/Berlin" in rendered


@pytest.mark.asyncio
async def test_digest_now_invokes_flush():
    digest_svc = AsyncMock()
    digest_svc.flush_user = AsyncMock(return_value=3)
    update = _build_update(42, [])
    context = _build_context([], digest_service=digest_svc)
    await digest_now_command(update, context)
    digest_svc.flush_user.assert_awaited_once_with(user_id=42)
    msg = update.message.reply_text.call_args.args[0]
    assert "3" in msg


# ── Regression coverage for read-before-write
# in mute/unmute and for input-validation guards.


@pytest.mark.asyncio
async def test_mute_preserves_existing_prefs():
    """Muting must not clobber digest_mode / timezone / throttle on the row."""
    from price_tracker.db.models import NotificationPrefs

    existing = NotificationPrefs(
        user_id=42,
        product_id=None,
        digest_mode=True,
        timezone="Europe/Berlin",
        throttle_per_hour=5,
    )
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=existing)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["all", "24"])
    context = _build_context(["all", "24"], repository=repo)
    await mute_command(update, context)
    repo.get_notification_prefs.assert_awaited_once_with(user_id=42, product_id=None)
    args, _ = repo.upsert_notification_prefs.call_args
    prefs = args[0]
    assert prefs.digest_mode is True
    assert prefs.timezone == "Europe/Berlin"
    assert prefs.throttle_per_hour == 5
    assert prefs.mute is True
    assert prefs.mute_until is not None


@pytest.mark.asyncio
async def test_unmute_preserves_existing_prefs():
    """Unmuting must not clobber digest_mode / timezone / throttle on the row."""
    from datetime import UTC, datetime, timedelta

    from price_tracker.db.models import NotificationPrefs

    existing = NotificationPrefs(
        user_id=42,
        product_id=None,
        mute=True,
        mute_until=datetime.now(UTC) + timedelta(hours=12),
        digest_mode=True,
        timezone="Europe/Berlin",
        throttle_per_hour=5,
    )
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=existing)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["all"])
    context = _build_context(["all"], repository=repo)
    await unmute_command(update, context)
    repo.get_notification_prefs.assert_awaited_once_with(user_id=42, product_id=None)
    args, _ = repo.upsert_notification_prefs.call_args
    prefs = args[0]
    assert prefs.digest_mode is True
    assert prefs.timezone == "Europe/Berlin"
    assert prefs.throttle_per_hour == 5
    assert prefs.mute is False
    assert prefs.mute_until is None


@pytest.mark.asyncio
async def test_mute_negative_hours_rejected():
    """Negative duration must be rejected without persisting."""
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["all", "-5"])
    context = _build_context(["all", "-5"], repository=repo)
    await mute_command(update, context)
    repo.upsert_notification_prefs.assert_not_called()
    msg = update.message.reply_text.call_args.args[0].lower()
    assert "positive" in msg or "must be" in msg


@pytest.mark.asyncio
async def test_quiet_hours_same_start_end_rejected():
    """An empty quiet-hours window (start==end) must be rejected."""
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["10:00-10:00"])
    context = _build_context(["10:00-10:00"], repository=repo)
    await quiet_hours_command(update, context)
    repo.upsert_notification_prefs.assert_not_called()
    msg = update.message.reply_text.call_args.args[0].lower()
    assert "same" in msg or "cannot" in msg


@pytest.mark.asyncio
async def test_prefs_rejects_zero_or_negative_product_id():
    """Explicit product_id <= 0 must be rejected, not silently coerced to global."""
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    update = _build_update(42, ["0"])
    context = _build_context(["0"], repository=repo)
    await prefs_command(update, context)
    # PreferencesManager.resolve calls get_notification_prefs internally; it must
    # never be reached when validation rejects the product_id.
    repo.get_notification_prefs.assert_not_called()
    msg = update.message.reply_text.call_args.args[0].lower()
    assert "positive integer" in msg


@pytest.mark.asyncio
async def test_digest_mode_invalid_interval_rejected():
    """Non-integer interval_min must be rejected without persisting."""
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.upsert_notification_prefs = AsyncMock()
    update = _build_update(42, ["on", "abc"])
    context = _build_context(["on", "abc"], repository=repo)
    await digest_mode_command(update, context)
    repo.upsert_notification_prefs.assert_not_called()
    msg = update.message.reply_text.call_args.args[0].lower()
    assert "positive integer" in msg


def test_timezone_command_uses_cached_set():
    """Sanity check: module-level cache exists and contains a known tz."""
    assert isinstance(_VALID_TIMEZONES, frozenset)
    assert "Europe/Berlin" in _VALID_TIMEZONES
