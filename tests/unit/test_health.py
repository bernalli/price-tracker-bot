# tests/unit/test_health.py
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time

from price_tracker.core.health import (
    HealthManager,
    QuarantineState,
)
from price_tracker.db.models import ScraperHealth


@pytest.fixture
def repo_mock() -> AsyncMock:
    repo = AsyncMock()
    repo.get_scraper_health = AsyncMock(return_value=None)
    repo.upsert_scraper_health = AsyncMock(return_value=None)
    repo.list_all_scraper_health = AsyncMock(return_value=[])
    return repo


class TestHealthManagerStateMachine:
    @pytest.mark.asyncio
    async def test_initial_state_is_closed(self, repo_mock):
        mgr = HealthManager(repo=repo_mock)
        await mgr.load()
        assert mgr.state("amazon.com") == QuarantineState.CLOSED

    @pytest.mark.asyncio
    async def test_three_blocks_transition_to_locked_t1(self, repo_mock):
        mgr = HealthManager(repo=repo_mock)
        await mgr.load()
        for _ in range(3):
            await mgr.record_block("xteink.com", reason="HTTP 429")
        assert mgr.state("xteink.com") == QuarantineState.LOCKED_T1
        assert mgr.is_locked("xteink.com")

    @pytest.mark.asyncio
    async def test_two_blocks_stays_closed(self, repo_mock):
        mgr = HealthManager(repo=repo_mock)
        await mgr.load()
        for _ in range(2):
            await mgr.record_block("x.com", reason="HTTP 429")
        assert mgr.state("x.com") == QuarantineState.CLOSED

    @pytest.mark.asyncio
    async def test_success_resets_counter(self, repo_mock):
        mgr = HealthManager(repo=repo_mock)
        await mgr.load()
        await mgr.record_block("x.com", reason="HTTP 429")
        await mgr.record_block("x.com", reason="HTTP 429")
        await mgr.record_success("x.com")
        # third block from zero counter — should still be CLOSED
        await mgr.record_block("x.com", reason="HTTP 429")
        assert mgr.state("x.com") == QuarantineState.CLOSED

    @pytest.mark.asyncio
    async def test_six_blocks_total_transitions_to_locked_t2(self, repo_mock):
        mgr = HealthManager(repo=repo_mock)
        await mgr.load()
        for _ in range(6):
            await mgr.record_block("x.com", reason="HTTP 429")
        assert mgr.state("x.com") == QuarantineState.LOCKED_T2

    @pytest.mark.asyncio
    async def test_twelve_blocks_total_transitions_to_locked_t3(self, repo_mock):
        mgr = HealthManager(repo=repo_mock)
        await mgr.load()
        for _ in range(12):
            await mgr.record_block("x.com", reason="HTTP 429")
        assert mgr.state("x.com") == QuarantineState.LOCKED_T3

    @pytest.mark.asyncio
    async def test_locked_t1_lockout_is_one_hour(self, repo_mock):
        with freeze_time("2026-05-09 12:00:00", tz_offset=0):
            mgr = HealthManager(repo=repo_mock)
            await mgr.load()
            for _ in range(3):
                await mgr.record_block("x.com", reason="HTTP 429")
            until = mgr.locked_until("x.com")
            assert until is not None
            assert until == datetime(2026, 5, 9, 13, 0, 0, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_locked_t2_lockout_is_six_hours(self, repo_mock):
        with freeze_time("2026-05-09 12:00:00", tz_offset=0):
            mgr = HealthManager(repo=repo_mock)
            await mgr.load()
            for _ in range(6):
                await mgr.record_block("x.com", reason="HTTP 429")
            assert mgr.locked_until("x.com") == datetime(2026, 5, 9, 18, 0, 0, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_locked_t3_lockout_is_24_hours(self, repo_mock):
        with freeze_time("2026-05-09 12:00:00", tz_offset=0):
            mgr = HealthManager(repo=repo_mock)
            await mgr.load()
            for _ in range(12):
                await mgr.record_block("x.com", reason="HTTP 429")
            assert mgr.locked_until("x.com") == datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_lockout_expiry_transitions_to_half_open(self, repo_mock):
        with freeze_time("2026-05-09 12:00:00") as frozen:
            mgr = HealthManager(repo=repo_mock)
            await mgr.load()
            for _ in range(3):
                await mgr.record_block("x.com", reason="HTTP 429")
            assert mgr.state("x.com") == QuarantineState.LOCKED_T1
            frozen.move_to("2026-05-09 13:01:00")
            assert mgr.state("x.com") == QuarantineState.HALF_OPEN_T1
            assert not mgr.is_locked("x.com")
            assert mgr.is_half_open("x.com")

    @pytest.mark.asyncio
    async def test_half_open_success_resets_to_closed(self, repo_mock):
        with freeze_time("2026-05-09 12:00:00") as frozen:
            mgr = HealthManager(repo=repo_mock)
            await mgr.load()
            for _ in range(3):
                await mgr.record_block("x.com", reason="HTTP 429")
            frozen.move_to("2026-05-09 13:01:00")
            await mgr.record_success("x.com")
            assert mgr.state("x.com") == QuarantineState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_block_promotes_to_next_tier(self, repo_mock):
        with freeze_time("2026-05-09 12:00:00") as frozen:
            mgr = HealthManager(repo=repo_mock)
            await mgr.load()
            for _ in range(3):
                await mgr.record_block("x.com", reason="HTTP 429")
            frozen.move_to("2026-05-09 13:01:00")
            await mgr.record_block("x.com", reason="HTTP 429")
            assert mgr.state("x.com") == QuarantineState.LOCKED_T2

    @pytest.mark.asyncio
    async def test_locked_t3_does_not_escalate_beyond(self, repo_mock):
        with freeze_time("2026-05-09 12:00:00") as frozen:
            mgr = HealthManager(repo=repo_mock)
            await mgr.load()
            for _ in range(12):
                await mgr.record_block("x.com", reason="HTTP 429")
            frozen.move_to("2026-05-10 12:01:00")
            await mgr.record_block("x.com", reason="HTTP 429")
            assert mgr.state("x.com") == QuarantineState.LOCKED_T3

    @pytest.mark.asyncio
    async def test_record_block_persists_via_repo(self, repo_mock):
        mgr = HealthManager(repo=repo_mock)
        await mgr.load()
        await mgr.record_block("x.com", reason="HTTP 429")
        repo_mock.upsert_scraper_health.assert_awaited()

    @pytest.mark.asyncio
    async def test_load_restores_state_from_repo(self, repo_mock):
        existing = ScraperHealth(
            domain="x.com",
            state="LOCKED_T2",
            consecutive_blocks=6,
            locked_until=datetime(2099, 1, 1, tzinfo=UTC),
            last_block_at=datetime(2026, 5, 9, tzinfo=UTC),
            last_block_reason="HTTP 429",
        )
        repo_mock.list_all_scraper_health = AsyncMock(return_value=[existing])
        mgr = HealthManager(repo=repo_mock)
        await mgr.load()
        assert mgr.state("x.com") == QuarantineState.LOCKED_T2
        assert mgr.is_locked("x.com")
