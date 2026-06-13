"""Unit tests for DigestService — enqueue, flush_user, flush_due."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time

from price_tracker.db.models import DigestEntry
from price_tracker.notifier.digest import DigestService


@pytest.fixture
def repo_mock() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def telegram_mock() -> AsyncMock:
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_enqueue_writes_via_repo(repo_mock: AsyncMock, telegram_mock: AsyncMock) -> None:
    repo_mock.enqueue_digest = AsyncMock(return_value=1)
    svc = DigestService(repo=repo_mock, bot=telegram_mock)
    await svc.enqueue(user_id=1, product_id=10, payload={"price": "99.0"})
    repo_mock.enqueue_digest.assert_awaited_once()


@pytest.mark.asyncio
async def test_flush_sends_message_and_marks_flushed(
    repo_mock: AsyncMock, telegram_mock: AsyncMock
) -> None:
    entries = [
        DigestEntry(
            id=1,
            user_id=42,
            product_id=10,
            alert_payload_json=json.dumps(
                {
                    "product_name": "Widget",
                    "old_price": "120",
                    "new_price": "99",
                    "currency": "EUR",
                    "domain": "amazon.it",
                }
            ),
            enqueued_at=datetime.now(UTC),
        ),
    ]
    repo_mock.list_pending_digest = AsyncMock(return_value=entries)
    repo_mock.mark_digest_flushed = AsyncMock()
    svc = DigestService(repo=repo_mock, bot=telegram_mock)
    flushed = await svc.flush_user(user_id=42)
    assert flushed == 1
    telegram_mock.send_message.assert_awaited()
    text = telegram_mock.send_message.call_args.kwargs["text"]
    assert "Widget" in text
    assert "amazon.it" in text
    repo_mock.mark_digest_flushed.assert_awaited_once_with([1])


@pytest.mark.asyncio
async def test_flush_no_pending_does_nothing(
    repo_mock: AsyncMock, telegram_mock: AsyncMock
) -> None:
    repo_mock.list_pending_digest = AsyncMock(return_value=[])
    svc = DigestService(repo=repo_mock, bot=telegram_mock)
    flushed = await svc.flush_user(user_id=1)
    assert flushed == 0
    telegram_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_flush_at_quiet_hours_end_via_scheduler(
    repo_mock: AsyncMock, telegram_mock: AsyncMock
) -> None:
    entries = [
        DigestEntry(
            id=1,
            user_id=42,
            product_id=10,
            alert_payload_json=json.dumps({"product_name": "X"}),
            enqueued_at=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        )
    ]
    repo_mock.list_pending_digest = AsyncMock(return_value=entries)
    repo_mock.mark_digest_flushed = AsyncMock()
    repo_mock.list_users_with_pending_digest = AsyncMock(
        return_value=[(42, datetime(2026, 5, 9, 12, 0, tzinfo=UTC))]
    )
    repo_mock.get_notification_prefs = AsyncMock(return_value=None)  # fall back to interval_minutes
    svc = DigestService(repo=repo_mock, bot=telegram_mock)
    with freeze_time("2026-05-09 13:01:00"):
        await svc.flush_due(interval_minutes=60)
    repo_mock.mark_digest_flushed.assert_awaited()
