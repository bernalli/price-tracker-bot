"""Integration test for scheduled price check (no real network)."""

# mypy: disable-error-code="method-assign,assignment,operator"
# Tests intentionally replace HealthManager methods on AsyncMock(spec=...) instances
# (lambda assignments to is_locked/is_half_open) — mypy can't validate cleanly.

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from price_tracker.core.health import HealthManager
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from price_tracker.db.models import ProductRecord

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


class _StubScraper(AbstractScraper):
    name = "stub"
    priority = 100

    def __init__(self, response: ProductInfo) -> None:
        self._response = response
        self.calls = 0

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        self.calls += 1
        return self._response


@pytest_asyncio.fixture
async def repo_with_product() -> AsyncIterator[tuple[Repository, int]]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    pid = await repo.add_product(
        user_id=1,
        url="https://example.com/p/1",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    try:
        yield repo, pid
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_scheduler_updates_price_on_drop(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.current_price == Decimal("80")
    assert stub.calls == 1


@pytest.mark.asyncio
async def test_scheduler_increments_errors_on_scrape_failure(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(error="HTTP 429"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 1


@pytest.mark.asyncio
async def test_scheduler_triggers_alert_on_threshold_drop(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    notifier.assert_awaited_once()


class _RaisingScraper(AbstractScraper):
    """Scraper that raises an httpx error to exercise the run_check_for_user except branch."""

    name = "raising"
    priority = 100

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        raise httpx.ConnectError("simulated network failure")


@pytest.mark.asyncio
async def test_scheduler_handles_scraper_exception_increments_errors(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Lines 55-57: httpx.HTTPError from scrape → increment_errors + log."""
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(_RaisingScraper())
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 1


@pytest.mark.asyncio
async def test_scheduler_run_check_all_iterates_users(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Lines 62-64: run_check_all iterates list_active_users."""
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_all()
    p = await repo.get_product(pid)
    assert p is not None
    assert p.current_price == Decimal("80")
    assert stub.calls == 1


@pytest.mark.asyncio
async def test_scheduler_skips_when_no_scraper_resolves(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Lines 73-74: registry.resolve returns None → log + return."""
    repo, pid = repo_with_product
    registry = ScraperRegistry()  # empty registry → resolve(url) returns None
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    p = await repo.get_product(pid)
    assert p is not None
    # Price unchanged, no error counter increment (no exception raised)
    assert p.current_price is None or p.current_price == Decimal("100")


@pytest.mark.asyncio
async def test_scheduler_outlier_price_rejected(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Lines 85-93: outlier read → log + return without updating price."""
    repo, pid = repo_with_product
    # Seed history: 6 points around 100 (>= MIN_HISTORY=5)
    for v in (100, 102, 98, 105, 100, 99):
        await repo.add_price_history(pid, Decimal(str(v)))

    # Scraper returns wildly inflated price (10x median) → outlier
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("1000"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    p = await repo.get_product(pid)
    assert p is not None
    # current_price NOT updated to 1000 (outlier rejected)
    assert p.current_price != Decimal("1000")
    # Notifier NOT called
    notifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_first_check_no_old_price_no_alert() -> None:
    """Line 101: when old_price is None, return without crossing threshold check.

    Build a product with NULL initial_price to force old_price=None branch.
    """
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    pid = await repo.add_product(
        user_id=1,
        url="https://example.com/p/no-init",
        name="NoInit",
        domain="example.com",
        initial_price=None,
        currency="EUR",
    )
    try:
        stub = _StubScraper(ProductInfo(name="NoInit", price=Decimal("80"), currency="EUR"))
        registry = ScraperRegistry()
        registry.register(stub)
        notifier = AsyncMock()
        async with httpx.AsyncClient() as client:
            scheduler = Scheduler(
                SchedulerDeps(
                    repo=repo,
                    registry=registry,
                    client=client,
                    notifier=notifier,
                    max_consecutive_errors=10,
                    delay_between_products=0.0,
                )
            )
            await scheduler.run_check_for_user(user_id=1)
        notifier.assert_not_awaited()
        # Price WAS updated despite no alert
        p = await repo.get_product(pid)
        assert p is not None
        assert p.current_price == Decimal("80")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_scheduler_skips_inactive_product(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Line 69: _check_product returns early when product is inactive."""
    repo, pid = repo_with_product
    await repo.pause_product(pid)

    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80")))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        # Call _check_product directly: list_products_for_user(only_active=True)
        # would skip the inactive product upstream; we want to hit line 69.
        await scheduler._check_product(pid)
    assert stub.calls == 0


@pytest.mark.asyncio
async def test_scheduler_price_none_increments_errors(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Lines 78-79: scraper returns price=None (e.g. parse failure) → increment_errors."""
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=None, error="parse failed"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 1


@pytest.mark.asyncio
async def test_scheduler_cleanup_old_history(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Line 123: cleanup_old_history delegates to repo.delete_old_price_history."""
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        deleted = await scheduler.cleanup_old_history(retention_days=365)
    # Empty history → 0 rows deleted
    assert deleted == 0


# ---------------------------------------------------------------------------
# item 8 fixtures: scheduler_factory + sample_products
# (now defined in tests/integration/conftest.py and shared across integration tests)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# item 8 tests: skip-on-locked + half-open single probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_skips_locked_domain(
    scheduler_factory: object,
    sample_products: list[ProductRecord],
) -> None:
    health_mgr: HealthManager = AsyncMock(spec=HealthManager)
    health_mgr.is_locked = lambda d: d == "xteink.com"
    health_mgr.is_half_open = lambda d: False

    scheduler: Scheduler = scheduler_factory(health_mgr=health_mgr)
    products = [p for p in sample_products if "xteink.com" in p.url]
    scrape_calls: list[str] = []
    scheduler._scrape_one = AsyncMock(side_effect=lambda p: scrape_calls.append(p.url))

    await scheduler._run_tick(products)

    assert scrape_calls == []  # all xteink products skipped


@pytest.mark.asyncio
async def test_scheduler_half_open_sends_only_one_probe(
    scheduler_factory: object,
    sample_products: list[ProductRecord],
) -> None:
    health_mgr: HealthManager = AsyncMock(spec=HealthManager)
    health_mgr.is_locked = lambda d: False
    half_open_for: set[str] = {"xteink.com"}
    health_mgr.is_half_open = lambda d: d in half_open_for

    scheduler: Scheduler = scheduler_factory(health_mgr=health_mgr)
    xteink_products = [p for p in sample_products if "xteink.com" in p.url]
    assert len(xteink_products) >= 2  # ensure multiple products on same domain

    calls: list[str] = []
    scheduler._scrape_one = AsyncMock(side_effect=lambda p: calls.append(p.url))
    await scheduler._run_tick(xteink_products)

    assert len(calls) == 1  # only one probe per half-open domain per tick


# ---------------------------------------------------------------------------
# item 16 tests: Prometheus metric emission from Scheduler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_sets_jobs_active_gauge(
    scheduler_factory: object,
    sample_products: list[ProductRecord],
) -> None:
    """`_run_tick` must set `scheduler_jobs_active` to the number of products
    in the current tick. `_scrape_one` is mocked because this test scopes
    only to the gauge — the success-counter path is covered end-to-end by the
    full pipeline integration tests (see `test_quarantine_flow.py`).
    """
    from prometheus_client import CollectorRegistry  # noqa: PLC0415

    from price_tracker.observability.metrics import MetricsRegistry  # noqa: PLC0415

    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    scheduler: Scheduler = scheduler_factory(metrics=metrics)
    scheduler._scrape_one = AsyncMock(return_value=None)

    await scheduler._run_tick(sample_products[:1])

    jobs_active = sum(
        sample.value
        for metric in reg.collect()
        if metric.name == "price_tracker_scheduler_jobs_active"
        for sample in metric.samples
    )
    assert jobs_active == 1


@pytest.mark.asyncio
async def test_scheduler_emits_quarantine_skip_total(
    scheduler_factory: object,
    sample_products: list[ProductRecord],
) -> None:
    from prometheus_client import CollectorRegistry  # noqa: PLC0415

    from price_tracker.observability.metrics import MetricsRegistry  # noqa: PLC0415

    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    health_mgr: HealthManager = AsyncMock(spec=HealthManager)
    health_mgr.is_locked = lambda _d: True
    health_mgr.is_half_open = lambda _d: False
    scheduler: Scheduler = scheduler_factory(metrics=metrics, health_mgr=health_mgr)
    await scheduler._run_tick(sample_products[:3])
    total = sum(
        sample.value
        for metric in reg.collect()
        if metric.name == "price_tracker_quarantine_skip"
        for sample in metric.samples
        if sample.name == "price_tracker_quarantine_skip_total"
    )
    assert total == 3  # all three sample products skipped


# ── Pull-mode methods (v0.1.6) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_check_one_product_for_user_returns_alert_on_threshold(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Pull mode: ``check_one_product_for_user`` returns CheckResult with alert
    set on threshold drop, and does NOT invoke the notifier (handler renders
    its own reply).
    """
    from price_tracker.core.scheduler import CheckResult  # noqa: PLC0415

    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        result = await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
    assert isinstance(result, CheckResult)
    assert result.product_id == pid
    assert result.user_id == 1
    assert result.alert is not None
    assert result.alert.new_price == Decimal("80")
    notifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_one_product_for_user_returns_none_on_no_drop(
    repo_with_product: tuple[Repository, int],
) -> None:
    """No threshold cross → CheckResult.alert is None (handler renders 'no change')."""
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("99"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        result = await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
    assert result.alert is None
    notifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_user_products_for_user_accumulates_results(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Pull mode: ``check_user_products_for_user`` returns one CheckResult per
    active product owned by the user. Notifier is never invoked.
    """
    repo, pid_first = repo_with_product
    # Add a second product to verify accumulation
    pid_second = await repo.add_product(
        user_id=1,
        url="https://example.com/p/2",
        name="Gadget",
        domain="example.com",
        initial_price=Decimal("50"),
        currency="EUR",
    )
    stub = _StubScraper(ProductInfo(name="Item", price=Decimal("40"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        results = await scheduler.check_user_products_for_user(user_id=1)
    assert len(results) == 2
    product_ids = {r.product_id for r in results}
    assert product_ids == {pid_first, pid_second}
    # Both crossed the 10% default threshold (100→40 and 50→40)
    alerts = [r.alert for r in results if r.alert is not None]
    assert len(alerts) == 2
    notifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_user_products_for_user_respects_locked_domain(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Quarantined domain → product skipped silently, no CheckResult emitted."""
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    health_mgr: HealthManager = AsyncMock(spec=HealthManager)
    health_mgr.is_locked = lambda _d: True
    health_mgr.is_half_open = lambda _d: False
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                delay_between_products=0.0,
                health_mgr=health_mgr,
            )
        )
        results = await scheduler.check_user_products_for_user(user_id=1)
    assert results == []
