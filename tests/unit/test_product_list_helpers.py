"""Unit tests for `_format_relative_time` used by /lista.

Regression: the DB stores naive timestamps (``YYYY-MM-DD HH:MM:SS``); the inline
``datetime.now(UTC) - fromisoformat(...)`` subtraction raised a naive-vs-aware
``TypeError`` that was swallowed, so the "🕐 Ultimo check" line silently vanished
for every product. The helper normalises naive timestamps to UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime

from price_tracker.bot.handlers._helpers import _format_relative_time


def test_relative_time_naive_timestamp_is_treated_as_utc():
    now = datetime(2026, 6, 15, 16, 30, tzinfo=UTC)
    assert _format_relative_time("2026-06-15 16:00:00", now=now) == "30min fa"


def test_relative_time_hours_and_days():
    now = datetime(2026, 6, 15, 16, 0, tzinfo=UTC)
    assert _format_relative_time("2026-06-15 13:00:00", now=now) == "3h fa"
    assert _format_relative_time("2026-06-13 16:00:00", now=now) == "2g fa"


def test_relative_time_aware_timestamp_with_z():
    now = datetime(2026, 6, 15, 16, 30, tzinfo=UTC)
    assert _format_relative_time("2026-06-15T16:00:00Z", now=now) == "30min fa"


def test_relative_time_none_or_garbage_returns_none():
    assert _format_relative_time(None) is None
    assert _format_relative_time("not-a-date") is None
