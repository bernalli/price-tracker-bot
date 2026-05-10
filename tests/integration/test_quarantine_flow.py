# tests/integration/test_quarantine_flow.py
"""Quarantine flow integration test — Bug #1 regression (xteink.com 429 loop).

Exercises the full pipeline:
  HealthManager state machine + Scheduler skip-on-locked + half-open probe
  from CLOSED through LOCKED_T1 → HALF_OPEN_T1 → LOCKED_T2.
"""

# mypy: disable-error-code="method-assign,assignment,operator"
# Tests intentionally replace HealthManager methods on AsyncMock(spec=...) instances
# (lambda assignments to is_locked/is_half_open) — mypy can't validate cleanly.

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time

from price_tracker.core.health import HealthManager, QuarantineState
from price_tracker.db.models import ProductRecord

if TYPE_CHECKING:
    from price_tracker.core.scheduler import Scheduler
    from price_tracker.db.repository import Repository

XTEINK_URL = "https://www.xteink.com/products/xteink-x3"


@pytest.fixture
async def health_mgr_with_repo(repository: Repository) -> HealthManager:
    mgr = HealthManager(repo=repository)
    await mgr.load()
    return mgr


def _make_product(pid: int, url: str) -> ProductRecord:
    """Build a minimal but fully-specified ProductRecord for testing."""
    return ProductRecord(
        id=pid,
        user_id=42,
        url=url,
        name="X" if pid == 1 else f"X{pid}",
        domain=None,
        initial_price=None,
        current_price=None,
        lowest_price=None,
        highest_price=None,
        target_price=None,
        threshold_type="drop_pct",
        threshold_value=Decimal("10"),
        is_active=True,
        is_available=True,
        consecutive_errors=0,
        currency="EUR",
        check_interval_minutes=None,
        last_checked_at=None,
        last_notified_at=None,
    )


@pytest.mark.asyncio
async def test_xteink_loop_does_not_recur(
    health_mgr_with_repo: HealthManager,
    scheduler_factory: object,
) -> None:
    """Regression for Bug #1: xteink.com 429 loop is broken by quarantine.

    Steps:
      1. Record 3 blocks → counter=3, LOCKED_T1
      2. 6 ticks during 1h lockout → scheduler must NOT scrape xteink.com at all
      3. Advance to 13:01 → HALF_OPEN_T1
      4. Tick with 2 products on same domain → exactly 1 probe
      5. Record still-429 → LOCKED_T2; verify locked_until = 19:01
    """
    mgr = health_mgr_with_repo

    with freeze_time("2026-05-09 12:00:00") as frozen:
        # Step 1 — three blocks → LOCKED_T1
        for _ in range(3):
            await mgr.record_block("xteink.com", reason="HTTP 429")
        assert mgr.state("xteink.com") == QuarantineState.LOCKED_T1
        assert mgr.is_locked("xteink.com")

        # Step 2 — scheduler must not hammer xteink.com during the 1h lock window
        scheduler: Scheduler = scheduler_factory(health_mgr=mgr)
        scrape_attempts: list[str] = []
        scheduler._scrape_one = AsyncMock(side_effect=lambda p: scrape_attempts.append(p.url))
        xteink_product = _make_product(1, XTEINK_URL)

        # Simulate 6 ticks (every 10 minutes) during lockout window
        for offset in range(0, 60, 10):
            frozen.move_to(datetime(2026, 5, 9, 12, offset, tzinfo=UTC))
            await scheduler._run_tick([xteink_product])
        assert scrape_attempts == [], "scheduler must not scrape locked domain"

        # Step 3 — advance past 1h lockout → HALF_OPEN_T1
        frozen.move_to("2026-05-09 13:01:00")
        assert mgr.state("xteink.com") == QuarantineState.HALF_OPEN_T1

        # Step 4 — tick with 2 products on same half-open domain → exactly 1 probe
        p2 = _make_product(2, f"{XTEINK_URL}-2")
        await scheduler._run_tick([xteink_product, p2])
        assert len(scrape_attempts) == 1, "half-open tick must send exactly one probe"

        # Step 5 — probe is still blocked → promote to LOCKED_T2
        await mgr.record_block("xteink.com", reason="HTTP 429")
        assert mgr.state("xteink.com") == QuarantineState.LOCKED_T2
        assert mgr.locked_until("xteink.com") == datetime(2026, 5, 9, 19, 1, tzinfo=UTC)
