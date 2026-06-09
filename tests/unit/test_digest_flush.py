"""Digest flush must be scheduled and respect per-user interval (#25)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

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
