"""Digest flush must be scheduled and respect per-user interval (#25)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from freezegun import freeze_time

from price_tracker.bot.messages import _, reset_locale, set_locale
from price_tracker.db.models import DigestEntry, NotificationPrefs
from price_tracker.main import digest_flush_job
from price_tracker.notifier.digest import DigestService


async def test_digest_flush_job_invokes_flush_due() -> None:
    digest_service = AsyncMock()
    context = MagicMock()
    context.bot_data = {"digest_service": digest_service}
    await digest_flush_job(context)
    digest_service.flush_due.assert_awaited_once()


async def test_digest_flush_job_noop_without_service() -> None:
    context = MagicMock()
    context.bot_data = {}
    await digest_flush_job(context)  # must not raise


async def test_periodic_flush_uses_configured_locale_and_restores_context() -> None:
    now = datetime.now(UTC)
    entry = DigestEntry(
        id=1,
        user_id=1,
        product_id=None,
        alert_payload_json=json.dumps(
            {
                "kind": "operational",
                "event": "suspended",
                "domain": "shop.example",
                "count": 3,
                "reason": "listing_gone",
            }
        ),
        enqueued_at=now - timedelta(hours=2),
    )
    repo = AsyncMock()
    repo.list_users_with_pending_digest = AsyncMock(return_value=[(1, now - timedelta(hours=2))])
    repo.get_notification_prefs = AsyncMock(return_value=None)
    repo.list_pending_digest = AsyncMock(return_value=[entry])
    repo.mark_digest_flushed = AsyncMock()
    bot = AsyncMock()
    svc = DigestService(repo=repo, bot=bot, lang="it_IT")

    caller_token = set_locale("en")
    try:
        await svc.flush_due(interval_minutes=60)
        text = bot.send_message.await_args.kwargs["text"]
        assert "Avvisi operativi" in text
        assert "prodotti" in text
        assert "dettagli" in text
        assert _("❌ Invalid ID.") == "❌ Invalid ID."
    finally:
        reset_locale(caller_token)


async def test_flush_due_respects_per_user_interval() -> None:
    now = datetime.now(UTC)
    repo = AsyncMock()
    # user 1 has waited 90 min, user 2 only 10 min
    repo.list_users_with_pending_digest = AsyncMock(
        return_value=[(1, now - timedelta(minutes=90)), (2, now - timedelta(minutes=10))]
    )

    def _prefs(*, user_id: int, product_id: int | None):  # noqa: ANN202, ARG001
        return MagicMock(digest_interval_minutes=60)

    repo.get_notification_prefs = AsyncMock(side_effect=_prefs)
    repo.list_pending_digest = AsyncMock(return_value=[])

    svc = DigestService(repo=repo, bot=AsyncMock())
    flushed_for = []
    original = svc.flush_user

    async def _spy(*, user_id: int) -> int:
        flushed_for.append(user_id)
        return await original(user_id=user_id)

    svc.flush_user = _spy  # type: ignore[method-assign]
    await svc.flush_due(interval_minutes=1440)

    assert flushed_for == [1]  # only the user past their 60-min interval


async def test_flush_due_skips_user_inside_quiet_hours() -> None:
    """A due digest remains queued until the user's quiet window has ended."""
    queued_at = datetime(2026, 5, 8, 0, 0, tzinfo=UTC)
    repo = AsyncMock()
    repo.list_users_with_pending_digest = AsyncMock(return_value=[(1, queued_at)])
    repo.get_notification_prefs = AsyncMock(
        return_value=NotificationPrefs(
            user_id=1,
            quiet_hours_start="22:00",
            quiet_hours_end="07:00",
            timezone="Europe/Rome",
        )
    )
    svc = DigestService(repo=repo, bot=AsyncMock())
    flushed_for: list[int] = []

    async def _spy(*, user_id: int) -> int:
        flushed_for.append(user_id)
        return 1

    svc.flush_user = _spy  # type: ignore[method-assign]

    with freeze_time("2026-05-09 00:00:00"):
        await svc.flush_due(interval_minutes=60)
    assert flushed_for == []

    with freeze_time("2026-05-09 06:00:00"):
        await svc.flush_due(interval_minutes=60)
    assert flushed_for == [1]


async def test_flush_due_user_without_prefs_is_flushed() -> None:
    """A user without preferences is never treated as being in quiet hours."""
    repo = AsyncMock()
    repo.list_users_with_pending_digest = AsyncMock(
        return_value=[(1, datetime(2026, 5, 8, 0, 0, tzinfo=UTC))]
    )
    repo.get_notification_prefs = AsyncMock(return_value=None)
    svc = DigestService(repo=repo, bot=AsyncMock())
    flushed_for: list[int] = []

    async def _spy(*, user_id: int) -> int:
        flushed_for.append(user_id)
        return 1

    svc.flush_user = _spy  # type: ignore[method-assign]

    with freeze_time("2026-05-09 06:00:00"):
        await svc.flush_due(interval_minutes=60)

    assert flushed_for == [1]
