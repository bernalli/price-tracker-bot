"""Every sweep that had work leaves one INFO line saying what it did."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest_asyncio

from price_tracker.core.exceptions import HTTPBlockStatus
from price_tracker.core.health import HealthManager
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import ScraperHealth
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src/price_tracker/db/migrations"
LOGGER = "price_tracker.core.scheduler"


class _ByUrl(AbstractScraper):
    name = "by-url"
    priority = 100

    def __init__(self, outcomes: dict[str, ProductInfo | Exception]) -> None:
        self._outcomes = outcomes

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        outcome = self._outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(user_id=1)
    try:
        yield repository
    finally:
        await conn.close()


async def _add(repo: Repository, url: str) -> int:
    return await repo.add_product(
        user_id=1,
        url=url,
        name=url.rsplit("/", 1)[-1],
        domain=None,
        initial_price=Decimal("100"),
        currency="EUR",
    )


def _summaries(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.getMessage().startswith("Sweep done")
    ]


async def _sweep(
    repo: Repository,
    outcomes: dict[str, ProductInfo | Exception],
    *,
    health: HealthManager | None = None,
) -> None:
    registry = ScraperRegistry()
    registry.register(_ByUrl(outcomes))
    async with httpx.AsyncClient() as client:
        deps = SchedulerDeps(
            repo=repo,
            registry=registry,
            client=client,
            notifier=AsyncMock(return_value=True),
            delay_between_products=0.0,
        )
        if health is not None:
            deps.health_mgr = health
        await Scheduler(deps).run_check_all()


async def test_summary_counts_outcomes_and_names_paused_stores(repo, caplog):
    ok_url = "https://open-shop.com/p/ok"
    bad_url = "https://open-shop.com/p/unreadable"
    refused_url = "https://refusing-shop.com/p/refused"
    locked_a = "https://locked-shop.com/p/a"
    locked_b = "https://locked-shop.com/p/b"
    for url in (ok_url, bad_url, refused_url, locked_a, locked_b):
        await _add(repo, url)
    health = HealthManager(repo)
    await health.load()
    health._records["locked-shop.com"] = ScraperHealth(
        domain="locked-shop.com",
        state="LOCKED_T3",
        consecutive_blocks=12,
        locked_until=datetime.now(UTC) + timedelta(hours=5),
    )
    outcomes: dict[str, ProductInfo | Exception] = {
        ok_url: ProductInfo(price=Decimal("90"), currency="EUR"),
        bad_url: ProductInfo(error="no price", stock_evidence=False),
        refused_url: HTTPBlockStatus(status=403, url=refused_url),
    }
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await _sweep(repo, outcomes, health=health)

    (summary,) = _summaries(caplog)
    assert "3 checked" in summary
    assert "ok=1" in summary
    assert "price_none=1" in summary
    assert "block=1" in summary
    assert "2 skipped" in summary
    assert "quarantined=2" in summary
    assert "locked-shop.com" in summary
    assert "refusing-shop.com" not in summary.split("skipped", 1)[1]


async def test_a_sweep_with_nothing_to_do_logs_no_summary(repo, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await _sweep(repo, {})
    assert _summaries(caplog) == []


async def test_summary_carries_no_product_name_or_url(repo, caplog):
    url = "https://open-shop.com/p/secret-gift-idea"
    await _add(repo, url)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await _sweep(repo, {url: ProductInfo(price=Decimal("90"), currency="EUR")})
    (summary,) = _summaries(caplog)
    assert "secret-gift-idea" not in summary
    assert "1 checked" in summary
    assert "0 skipped" in summary
