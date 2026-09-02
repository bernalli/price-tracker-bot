"""Unit tests for DigestService — enqueue, flush_user, flush_due."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time

from price_tracker.core.textlimits import visible_length
from price_tracker.db.models import DigestEntry
from price_tracker.notifier.digest import DigestService, _digest_blocks


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
async def test_flush_due_uses_interval_when_no_prefs(
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


def _entry(*, entry_id: int, product_id: int | None, payload: object) -> DigestEntry:
    """Create a digest row with a JSON payload unless raw text is supplied."""
    serialized = payload if isinstance(payload, str) else json.dumps(payload)
    return DigestEntry(
        id=entry_id,
        user_id=42,
        product_id=product_id,
        alert_payload_json=serialized,
        enqueued_at=datetime.now(UTC),
    )


def test_digest_renders_operational_section() -> None:
    """Price changes and operational notices render in separate digest sections."""
    header, blocks, footer, unrenderable_ids = _digest_blocks(
        [
            _entry(
                entry_id=1,
                product_id=10,
                payload={
                    "product_name": "Widget",
                    "old_price": "120",
                    "new_price": "99",
                    "currency": "EUR",
                    "domain": "shop.example",
                },
            ),
            _entry(
                entry_id=2,
                product_id=None,
                payload={
                    "kind": "operational",
                    "event": "suspended",
                    "domain": "shop.example",
                    "count": 2,
                    "reason": "listing_gone",
                },
            ),
            _entry(
                entry_id=3,
                product_id=None,
                payload={
                    "kind": "operational",
                    "event": "warning",
                    "domain": "shop.example",
                    "count": 2,
                    "max": 10,
                },
            ),
        ]
    )

    text = "\n".join([header, *(block for _, block in blocks), footer])
    assert "1 price change" in header
    assert "⚠️ Operational notices" in text
    assert "shop.example — 2 products: tracking suspended (page not found (HTTP 404))" in text
    assert "shop.example — 2 products: checks failing (2/10)" in text
    assert "Use /reactivate or /errori for details." in footer
    assert unrenderable_ids == []


def test_digest_only_operational_entries() -> None:
    """An operational-only queue reports zero price changes."""
    header, blocks, footer, unrenderable_ids = _digest_blocks(
        [
            _entry(
                entry_id=1,
                product_id=None,
                payload={"kind": "operational", "event": "quarantine", "domain": "shop.example"},
            )
        ]
    )

    assert "0 price changes" in header
    assert "⚠️ Operational notices" in "\n".join(block for _, block in blocks)
    assert "Use /reactivate or /errori for details." in footer
    assert unrenderable_ids == []


def test_digest_operational_entry_without_fields_does_not_crash() -> None:
    """Sparse operational payloads use conservative rendering fallbacks."""
    _, blocks, _, unrenderable_ids = _digest_blocks(
        [_entry(entry_id=1, product_id=None, payload={"kind": "operational"})]
    )

    assert blocks == [
        (1, "⚠️ Operational notices\nunknown — 1 products: tracking suspended (check failed)")
    ]
    assert unrenderable_ids == []


def test_digest_quarantine_entry() -> None:
    """Quarantine entries use their dedicated concise copy."""
    _, blocks, _, _ = _digest_blocks(
        [
            _entry(
                entry_id=1,
                product_id=None,
                payload={"kind": "operational", "event": "quarantine", "domain": "shop.example"},
            )
        ]
    )

    assert blocks == [(1, "⚠️ Operational notices\nshop.example — quarantined")]


def test_digest_price_entry_with_null_product_id_uses_fallback_name() -> None:
    """Price payloads without a product owner retain a readable fallback name."""
    _, blocks, _, _ = _digest_blocks(
        [
            _entry(
                entry_id=1,
                product_id=None,
                payload={"old_price": "120", "new_price": "99", "currency": "EUR"},
            )
        ]
    )

    assert "Operational notice" in blocks[0][1]


def test_digest_blocks_reports_unrenderable_entries() -> None:
    """Unreadable JSON rows are reported for quarantine rather than retained forever."""
    _, blocks, _, unrenderable_ids = _digest_blocks(
        [
            _entry(entry_id=1, product_id=10, payload={"product_name": "Widget"}),
            _entry(entry_id=2, product_id=11, payload="{not-json"),
        ]
    )

    assert [entry_id for entry_id, _ in blocks] == [1]
    assert unrenderable_ids == [2]


@pytest.mark.asyncio
async def test_flush_user_quarantines_unrenderable_entries(
    repo_mock: AsyncMock, telegram_mock: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Invalid payloads are flushed and logged, even if they are the whole queue."""
    mixed_entries = [
        _entry(entry_id=1, product_id=10, payload={"product_name": "Widget"}),
        _entry(entry_id=2, product_id=11, payload="{not-json"),
    ]
    repo_mock.list_pending_digest = AsyncMock(return_value=mixed_entries)
    repo_mock.mark_digest_flushed = AsyncMock()
    svc = DigestService(repo=repo_mock, bot=telegram_mock)

    with caplog.at_level("WARNING"):
        flushed = await svc.flush_user(user_id=42)

    assert flushed == 2
    repo_mock.mark_digest_flushed.assert_awaited_once_with([1, 2])
    assert "entry_id=2" in caplog.text
    assert "user_id=42" in caplog.text

    repo_mock.list_pending_digest = AsyncMock(
        return_value=[_entry(entry_id=3, product_id=12, payload="{not-json")]
    )
    repo_mock.mark_digest_flushed.reset_mock()
    telegram_mock.send_message.reset_mock()

    flushed = await svc.flush_user(user_id=42)

    assert flushed == 1
    telegram_mock.send_message.assert_not_awaited()
    repo_mock.mark_digest_flushed.assert_awaited_once_with([3])


@pytest.mark.asyncio
async def test_flush_user_paginates_and_marks_only_sent_pages(
    repo_mock: AsyncMock, telegram_mock: AsyncMock
) -> None:
    """A failed page leaves that page and all later pages pending for retry."""
    entries = [
        _entry(
            entry_id=entry_id,
            product_id=entry_id,
            payload={
                "product_name": "x" * 200,
                "old_price": "120",
                "new_price": "99",
                "domain": "example" * 10,
            },
        )
        for entry_id in range(1, 51)
    ]
    repo_mock.list_pending_digest = AsyncMock(return_value=entries)
    repo_mock.mark_digest_flushed = AsyncMock()
    svc = DigestService(repo=repo_mock, bot=telegram_mock)

    flushed = await svc.flush_user(user_id=42)

    assert flushed == 50
    assert telegram_mock.send_message.await_count >= 2
    assert all(
        visible_length(call.kwargs["text"]) <= 4000
        for call in telegram_mock.send_message.await_args_list
    )
    marked_ids = [
        entry_id
        for call in repo_mock.mark_digest_flushed.await_args_list
        for entry_id in call.args[0]
    ]
    assert marked_ids == list(range(1, 51))
    assert (
        len(repo_mock.mark_digest_flushed.await_args_list) == telegram_mock.send_message.await_count
    )

    repo_mock.mark_digest_flushed.reset_mock()
    telegram_mock.send_message.reset_mock()
    telegram_mock.send_message.side_effect = [None, RuntimeError("Telegram unavailable")]

    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        await svc.flush_user(user_id=42)

    assert repo_mock.mark_digest_flushed.await_count == 1
    assert repo_mock.mark_digest_flushed.await_args.args[0] != list(range(1, 51))
