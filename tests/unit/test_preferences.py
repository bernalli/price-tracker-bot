"""Tests for PreferencesManager + EffectivePrefs resolution chain."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from price_tracker.db.models import NotificationPrefs
from price_tracker.notifier.preferences import EffectivePrefs, PreferencesManager


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
