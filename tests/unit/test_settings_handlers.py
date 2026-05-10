"""Tests for the notification preference handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.settings import (
    digest_mode_command,
    digest_now_command,
    mute_command,
    prefs_command,
    quiet_hours_command,
    throttle_command,
    timezone_command,
    unmute_command,  # noqa: F401  — imported per spec for symbol-presence check
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
    context.bot_data = bot_data
    return context


@pytest.mark.asyncio
async def test_mute_all_forever():
    repo = AsyncMock()
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
