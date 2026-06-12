"""Unit tests for debug handler commands: health_command, status_command."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.debug import health_command, status_command
from price_tracker.core.health import HealthManager
from price_tracker.db.models import ScraperHealth


def _make_context(
    *,
    health_records: list[ScraperHealth],
    is_admin: bool = True,
) -> MagicMock:
    """Build a minimal ContextTypes.DEFAULT_TYPE mock for health_command tests.

    The real admin_only decorator calls _db(context).is_user_admin(user_id),
    so we wire a mock DB that returns the desired admin bool. The health
    manager is a real HealthManager (records injected) so the handler sees
    the genuine effective-state logic (lockout expiry -> HALF_OPEN on read).
    """
    db_mock = MagicMock()
    db_mock.is_user_admin = AsyncMock(return_value=is_admin)

    health_mgr = HealthManager(repo=MagicMock())
    health_mgr._records = {r.domain: r for r in health_records}

    context = MagicMock()
    context.bot_data = {
        "db": db_mock,
        "health_manager": health_mgr,
    }
    return context


@pytest.mark.asyncio
async def test_health_command_renders_locked_and_half_open() -> None:
    """Admin calling /health sees locked + half-open domains in the report."""
    update = MagicMock()
    update.effective_user.id = 12345
    update.message.reply_html = AsyncMock()

    now = datetime.now(UTC)
    records = [
        ScraperHealth(domain="amazon.com", state="CLOSED", last_success_at=now),
        ScraperHealth(
            domain="xteink.com",
            state="LOCKED_T3",
            consecutive_blocks=12,
            locked_until=now + timedelta(hours=18, minutes=42),
            last_block_at=now,
            last_block_reason="HTTP 429",
        ),
        ScraperHealth(
            domain="aliexpress.com",
            state="HALF_OPEN_T1",
            consecutive_blocks=3,
            last_block_at=now,
            last_block_reason="HTTP 429",
        ),
    ]
    context = _make_context(health_records=records, is_admin=True)

    await health_command(update, context)

    update.message.reply_html.assert_awaited_once()
    rendered: str = update.message.reply_html.call_args.args[0]

    assert "Scraper Health Report" in rendered
    assert "xteink.com" in rendered
    assert "T3" in rendered
    assert "aliexpress.com" in rendered
    # CLOSED domain either omitted from problem lists or summarised as healthy count
    assert "xteink.com" in rendered or "Locked" in rendered
    # amazon.com only appears in healthy count summary, not in problem lists
    locked_section = rendered[rendered.find("Locked:") :] if "Locked:" in rendered else ""
    assert "amazon.com" not in locked_section


@pytest.mark.asyncio
async def test_health_command_rejects_non_admin() -> None:
    """Non-admin calling /health receives a refusal message."""
    update = MagicMock()
    update.effective_user.id = 99999
    update.message.reply_text = AsyncMock()

    context = _make_context(health_records=[], is_admin=False)

    await health_command(update, context)

    update.message.reply_text.assert_awaited_once()
    msg: str = update.message.reply_text.call_args.args[0]
    assert "amministratore" in msg.lower() or "admin" in msg.lower() or "autorizzato" in msg.lower()


@pytest.mark.asyncio
async def test_health_command_all_healthy() -> None:
    """With all domains CLOSED, report shows only healthy count and no problem sections."""
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_html = AsyncMock()

    now = datetime.now(UTC)
    context = _make_context(
        health_records=[
            ScraperHealth(domain="amazon.com", state="CLOSED", last_success_at=now),
            ScraperHealth(domain="ebay.com", state="CLOSED", last_success_at=now),
        ],
        is_admin=True,
    )

    await health_command(update, context)

    rendered: str = update.message.reply_html.call_args.args[0]
    assert "Scraper Health Report" in rendered
    # Summary counts are present but the detailed problem sections must be absent
    assert "<b>Locked:</b>" not in rendered
    assert "<b>Half-open:</b>" not in rendered


@pytest.mark.asyncio
async def test_health_command_shows_recent_blocks() -> None:
    """Last block events appear in the rendered output."""
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_html = AsyncMock()

    now = datetime.now(UTC)
    context = _make_context(
        health_records=[
            ScraperHealth(
                domain="blocked.com",
                state="LOCKED_T1",
                consecutive_blocks=3,
                locked_until=now + timedelta(hours=1),
                last_block_at=now,
                last_block_reason="HTTP 403",
            )
        ],
        is_admin=True,
    )

    await health_command(update, context)

    rendered: str = update.message.reply_html.call_args.args[0]
    assert "HTTP 403" in rendered or "blocked.com" in rendered


@pytest.mark.asyncio
async def test_health_command_handles_locked_without_locked_until() -> None:
    """Regression: sort key must not crash when locked_until is None."""
    update = MagicMock()
    update.effective_user.id = 12345
    update.message.reply_html = AsyncMock()

    context = _make_context(
        health_records=[
            ScraperHealth(
                domain="x.com",
                state="LOCKED_T1",
                consecutive_blocks=3,
                locked_until=None,  # edge case: no expiry set
            ),
        ],
        is_admin=True,
    )

    await health_command(update, context)
    update.message.reply_html.assert_awaited()


@pytest.mark.asyncio
async def test_health_command_expired_lockout_listed_as_half_open() -> None:
    """Bug #60: an expired lockout is effectively half-open on read.

    The persisted row still says LOCKED_T1, but HealthManager.state()
    resolves it to HALF_OPEN_T1 once locked_until has passed — the report
    must follow the effective state, never print 'expires in expired'.
    """
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_html = AsyncMock()

    now = datetime.now(UTC)
    context = _make_context(
        health_records=[
            ScraperHealth(
                domain="expired.com",
                state="LOCKED_T1",
                consecutive_blocks=3,
                locked_until=now - timedelta(minutes=5),  # lockout already expired
                last_block_at=now - timedelta(hours=2),
                last_block_reason="HTTP 429",
            ),
        ],
        is_admin=True,
    )

    await health_command(update, context)

    rendered: str = update.message.reply_html.call_args.args[0]
    assert "Half-open: 1" in rendered
    assert "Locked: 0" in rendered
    assert "<b>Half-open:</b>" in rendered
    assert "expired.com — probing on next tick" in rendered
    assert "<b>Locked:</b>" not in rendered
    assert "expires in expired" not in rendered


@pytest.mark.asyncio
async def test_status_shows_uptime_and_products_tracked() -> None:
    """status_command renders uptime + products_tracked from metrics gauges."""
    update = MagicMock()
    update.message.reply_html = AsyncMock()
    metrics = MagicMock()
    metrics.bot_uptime_seconds._value.get = lambda: 3700.0  # ~1h 1m 40s
    metrics.products_tracked_total._value.get = lambda: 42
    context = MagicMock()
    context.bot_data = {"metrics": metrics}

    await status_command(update, context)

    update.message.reply_html.assert_awaited_once()
    rendered: str = update.message.reply_html.call_args.args[0]
    assert "1h" in rendered or "uptime" in rendered.lower()
    assert "42" in rendered
