"""Tests for PreferencesManager + EffectivePrefs resolution chain (F3.D Task 24)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from price_tracker.db.models import NotificationPrefs
from price_tracker.notifier.preferences import (
    EffectivePrefs,
    PreferencesManager,
    ThrottleWindow,
    is_muted_now,
    is_quiet_now,
)


@pytest.fixture
def repo_mock() -> AsyncMock:
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=None)
    return repo


@pytest.mark.asyncio
async def test_resolve_returns_defaults_when_no_rows(repo_mock: AsyncMock) -> None:
    mgr = PreferencesManager(repo=repo_mock)
    eff: EffectivePrefs = await mgr.resolve(user_id=1, product_id=10)
    assert eff.mute is False
    assert eff.digest_mode is False
    assert eff.digest_interval_minutes == 60
    assert eff.timezone == "Europe/Rome"
    assert eff.quiet_hours_start is None
    assert eff.throttle_per_hour is None


@pytest.mark.asyncio
async def test_resolve_uses_global_when_no_per_product(repo_mock: AsyncMock) -> None:
    repo_mock.get_notification_prefs = AsyncMock(
        side_effect=[
            None,  # per-product call
            NotificationPrefs(
                user_id=1, product_id=None, digest_mode=True, timezone="Europe/Berlin"
            ),
        ]
    )
    mgr = PreferencesManager(repo=repo_mock)
    eff = await mgr.resolve(user_id=1, product_id=10)
    assert eff.digest_mode is True
    assert eff.timezone == "Europe/Berlin"


@pytest.mark.asyncio
async def test_resolve_per_product_overrides_global(repo_mock: AsyncMock) -> None:
    repo_mock.get_notification_prefs = AsyncMock(
        side_effect=[
            NotificationPrefs(user_id=1, product_id=10, digest_mode=False, mute=True),
            NotificationPrefs(user_id=1, product_id=None, digest_mode=True, mute=False),
        ]
    )
    mgr = PreferencesManager(repo=repo_mock)
    eff = await mgr.resolve(user_id=1, product_id=10)
    assert eff.mute is True
    assert eff.digest_mode is False


@pytest.mark.asyncio
async def test_resolve_global_ignores_per_product_row(repo_mock: AsyncMock) -> None:
    repo_mock.get_notification_prefs = AsyncMock(
        return_value=NotificationPrefs(user_id=1, product_id=None, digest_mode=False)
    )
    mgr = PreferencesManager(repo=repo_mock)

    eff = await mgr.resolve_global(user_id=1)

    assert eff.digest_mode is False
    repo_mock.get_notification_prefs.assert_awaited_once_with(user_id=1, product_id=None)


@pytest.mark.asyncio
async def test_resolve_global_returns_defaults_without_row(repo_mock: AsyncMock) -> None:
    mgr = PreferencesManager(repo=repo_mock)

    eff = await mgr.resolve_global(user_id=1)

    assert eff == EffectivePrefs(
        mute=False,
        mute_until=None,
        digest_mode=False,
        digest_interval_minutes=60,
        quiet_hours_start=None,
        quiet_hours_end=None,
        throttle_per_hour=None,
        timezone="Europe/Rome",
    )


@pytest.mark.asyncio
async def test_resolve_falls_back_field_by_field(repo_mock: AsyncMock) -> None:
    repo_mock.get_notification_prefs = AsyncMock(
        side_effect=[
            NotificationPrefs(
                user_id=1,
                product_id=10,
                mute=True,
                quiet_hours_start=None,
                quiet_hours_end=None,
            ),
            NotificationPrefs(
                user_id=1,
                product_id=None,
                quiet_hours_start="22:00",
                quiet_hours_end="08:00",
            ),
        ]
    )
    mgr = PreferencesManager(repo=repo_mock)
    eff = await mgr.resolve(user_id=1, product_id=10)
    assert eff.mute is True
    assert eff.quiet_hours_start == "22:00"
    assert eff.quiet_hours_end == "08:00"


class TestIsQuietNow:
    def test_no_quiet_hours_set(self) -> None:
        eff = EffectivePrefs(
            mute=False,
            mute_until=None,
            digest_mode=False,
            digest_interval_minutes=60,
            quiet_hours_start=None,
            quiet_hours_end=None,
            throttle_per_hour=None,
            timezone="UTC",
        )
        assert not is_quiet_now(eff, now_utc=datetime(2026, 5, 9, 23, 0, tzinfo=UTC))

    def test_simple_window_inside(self) -> None:
        eff = EffectivePrefs(
            mute=False,
            mute_until=None,
            digest_mode=False,
            digest_interval_minutes=60,
            quiet_hours_start="13:00",
            quiet_hours_end="14:00",
            throttle_per_hour=None,
            timezone="UTC",
        )
        assert is_quiet_now(eff, now_utc=datetime(2026, 5, 9, 13, 30, tzinfo=UTC))

    def test_simple_window_outside(self) -> None:
        eff = EffectivePrefs(
            mute=False,
            mute_until=None,
            digest_mode=False,
            digest_interval_minutes=60,
            quiet_hours_start="13:00",
            quiet_hours_end="14:00",
            throttle_per_hour=None,
            timezone="UTC",
        )
        assert not is_quiet_now(eff, now_utc=datetime(2026, 5, 9, 14, 1, tzinfo=UTC))

    def test_wraps_midnight_inside_late(self) -> None:
        eff = EffectivePrefs(
            mute=False,
            mute_until=None,
            digest_mode=False,
            digest_interval_minutes=60,
            quiet_hours_start="22:00",
            quiet_hours_end="08:00",
            throttle_per_hour=None,
            timezone="UTC",
        )
        assert is_quiet_now(eff, now_utc=datetime(2026, 5, 9, 23, 30, tzinfo=UTC))

    def test_wraps_midnight_inside_early(self) -> None:
        eff = EffectivePrefs(
            mute=False,
            mute_until=None,
            digest_mode=False,
            digest_interval_minutes=60,
            quiet_hours_start="22:00",
            quiet_hours_end="08:00",
            throttle_per_hour=None,
            timezone="UTC",
        )
        assert is_quiet_now(eff, now_utc=datetime(2026, 5, 10, 3, 0, tzinfo=UTC))

    def test_timezone_shifts_window(self) -> None:
        eff = EffectivePrefs(
            mute=False,
            mute_until=None,
            digest_mode=False,
            digest_interval_minutes=60,
            quiet_hours_start="22:00",
            quiet_hours_end="08:00",
            throttle_per_hour=None,
            timezone="Europe/Rome",
        )
        # 22:00 Europe/Rome (CEST = UTC+2 in May) = 20:00 UTC
        assert is_quiet_now(eff, now_utc=datetime(2026, 5, 9, 20, 30, tzinfo=UTC))


class TestIsMutedNow:
    def test_mute_off(self) -> None:
        eff = EffectivePrefs(
            mute=False,
            mute_until=None,
            digest_mode=False,
            digest_interval_minutes=60,
            quiet_hours_start=None,
            quiet_hours_end=None,
            throttle_per_hour=None,
            timezone="UTC",
        )
        assert not is_muted_now(eff, now_utc=datetime.now(UTC))

    def test_mute_forever(self) -> None:
        eff = EffectivePrefs(
            mute=True,
            mute_until=None,
            digest_mode=False,
            digest_interval_minutes=60,
            quiet_hours_start=None,
            quiet_hours_end=None,
            throttle_per_hour=None,
            timezone="UTC",
        )
        assert is_muted_now(eff, now_utc=datetime.now(UTC))

    def test_mute_with_expiry_in_future(self) -> None:
        future = datetime(2099, 1, 1, tzinfo=UTC)
        eff = EffectivePrefs(
            mute=True,
            mute_until=future,
            digest_mode=False,
            digest_interval_minutes=60,
            quiet_hours_start=None,
            quiet_hours_end=None,
            throttle_per_hour=None,
            timezone="UTC",
        )
        assert is_muted_now(eff, now_utc=datetime(2026, 5, 9, tzinfo=UTC))

    def test_mute_with_expiry_passed(self) -> None:
        past = datetime(2020, 1, 1, tzinfo=UTC)
        eff = EffectivePrefs(
            mute=True,
            mute_until=past,
            digest_mode=False,
            digest_interval_minutes=60,
            quiet_hours_start=None,
            quiet_hours_end=None,
            throttle_per_hour=None,
            timezone="UTC",
        )
        assert not is_muted_now(eff, now_utc=datetime(2026, 5, 9, tzinfo=UTC))


class TestThrottleWindow:
    def test_initial_window_empty(self) -> None:
        w = ThrottleWindow()
        assert w.timestamps == []

    def test_serialize_roundtrip(self) -> None:
        w = ThrottleWindow(timestamps=[1715258400.0, 1715258500.0])
        data = w.to_json()
        loaded = ThrottleWindow.from_json(data)
        assert loaded.timestamps == w.timestamps

    def test_record_and_count_within_hour(self) -> None:
        now = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
        w = ThrottleWindow()
        w.record(now)
        w.record(now + timedelta(minutes=30))
        assert w.count_within_hour(now + timedelta(minutes=45)) == 2

    def test_evicts_old_entries(self) -> None:
        old = datetime(2026, 5, 9, 10, 0, tzinfo=UTC)
        new = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
        w = ThrottleWindow()
        w.record(old)
        # 2h later → eviction
        assert w.count_within_hour(new) == 0

    def test_throttle_exceeded_predicate(self) -> None:
        now = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
        w = ThrottleWindow()
        for i in range(5):
            w.record(now + timedelta(minutes=i))
        assert w.exceeded(limit=5, now=now + timedelta(minutes=10))
        assert not w.exceeded(limit=10, now=now + timedelta(minutes=10))
