"""Shared fixtures for integration tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.core.health import HealthManager
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from price_tracker.observability.metrics import MetricsRegistry

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[Repository]:
    """In-memory SQLite Repository with all migrations applied."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


def _make_no_op_health_mgr() -> HealthManager:
    """Return an AsyncMock(spec=HealthManager) with is_locked/is_half_open always False."""
    mgr: HealthManager = AsyncMock(spec=HealthManager)
    mgr.is_locked = lambda _d: False
    mgr.is_half_open = lambda _d: False
    return mgr


@pytest.fixture
def scheduler_factory() -> object:
    """Factory that builds a Scheduler with injectable health_mgr.

    Usage::

        scheduler = scheduler_factory(health_mgr=some_mock)
        # or without health_mgr for existing tests (no-op default)
        scheduler = scheduler_factory()
    """

    def _factory(
        *,
        health_mgr: HealthManager | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> Scheduler:
        if health_mgr is None:
            health_mgr = _make_no_op_health_mgr()
        deps = SchedulerDeps(
            repo=AsyncMock(),
            registry=AsyncMock(),
            client=AsyncMock(),
            notifier=AsyncMock(),
            max_consecutive_errors=10,
            delay_between_products=0.0,
            health_mgr=health_mgr,
            metrics=metrics,
        )
        return Scheduler(deps)

    return _factory


@pytest.fixture
def sample_products() -> list:
    """A mix of products across two domains: xteink and example.com (1)."""
    from price_tracker.db.models import ProductRecord  # noqa: PLC0415

    def _make(pid: int, url: str) -> ProductRecord:
        return ProductRecord(
            id=pid,
            user_id=1,
            url=url,
            name=f"Product {pid}",
            domain=None,
            initial_price=Decimal("100"),
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

    return [
        _make(1, "https://xteink.com/product/1"),
        _make(2, "https://xteink.com/product/2"),
        _make(3, "https://example.com/product/3"),
    ]
