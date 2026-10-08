"""Integration test for scheduled price check (no real network)."""

# mypy: disable-error-code="method-assign,assignment,operator"
# Tests intentionally replace HealthManager methods on AsyncMock(spec=...) instances
# (lambda assignments to is_locked/is_half_open) — mypy can't validate cleanly.

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.alert import format_operational_notice
from price_tracker.core.exceptions import ListingGone, ParseError
from price_tracker.core.health import HealthManager
from price_tracker.core.notices import NoticeCollector, NoticeGroup, OperationalEvent
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps, _failure_reason
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


class _ScriptedScraper(AbstractScraper):
    """Return or raise one scripted outcome per real scheduler invocation."""

    name = "scripted"
    priority = 100

    def __init__(self, outcomes: list[ProductInfo | BaseException]) -> None:
        self._outcomes = iter(outcomes)

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        outcome = next(self._outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/p/1")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"status {status}", request=request, response=response)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ListingGone(status=404, url="https://example.com/p/1"), ("listing_gone", "HTTP 404")),
        (_http_status_error(404), ("listing_gone", "HTTP 404")),
        (_http_status_error(410), ("listing_gone", "HTTP 410")),
        (_http_status_error(500), ("http_error", "status 500")),
        (ParseError("missing price"), ("parse_error", "missing price")),
        (KeyError("price"), ("http_error", "'price'")),
        (RuntimeError("boom"), ("unexpected", "boom")),
    ],
    ids=[
        "listing-gone",
        "http-404",
        "http-410",
        "http-500",
        "parse-error",
        "key-error",
        "unexpected",
    ],
)
def test_failure_reason_classification(exc: BaseException, expected: tuple[str, str]) -> None:
    assert _failure_reason(exc) == expected


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
async def test_scheduler_currency_mismatch_skips_persist_and_alert(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Bug #20: scraped currency != stored currency → warn + skip, no persist.

    A USD read against a EUR product (wrong variant / marketplace redirect)
    must not be persisted as price/history, must not alert, must not count
    as a scrape error, and must NOT silently rewrite the stored currency.
    """
    repo, pid = repo_with_product  # stored currency = EUR
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="USD"))
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
    assert p.current_price != Decimal("80")  # not persisted
    assert p.currency == "EUR"  # NO update_currency
    assert p.consecutive_errors == 0  # not a scrape failure
    history = await repo.get_price_history(pid, limit=50)
    assert all(h.price != Decimal("80") for h in history)  # no history row
    notifier.assert_not_awaited()  # no alert


@pytest.mark.asyncio
async def test_scheduler_currency_mismatch_resets_error_counters(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A parsed price in another currency proves the listing is still live."""
    repo, pid = repo_with_product
    await repo.record_failure(pid, reason="listing_gone", detail="HTTP 404")
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="USD"))
    registry = ScraperRegistry()
    registry.register(stub)
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)

    product = await repo.get_product(pid)
    assert product is not None
    assert product.consecutive_errors == 0
    assert product.gone_streak == 0


@pytest.mark.asyncio
async def test_scheduler_currency_mismatch_records_success_for_half_open_domain(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Regression of fix #20: a currency-mismatch read is still a SUCCESSFUL
    scrape (HTTP ok, price parsed) — only the persist/alert is skipped.

    When the mismatching product is the single probe of a HALF_OPEN domain,
    the guard must still call ``record_success`` so the domain can transition
    back to CLOSED. Before the fix the guard returned early without
    ``handle_success_in_pipeline``, so the same product consumed the probe
    slot on every sweep and the domain stayed HALF_OPEN forever.
    """
    repo, pid = repo_with_product  # stored currency = EUR
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="USD"))
    registry = ScraperRegistry()
    registry.register(stub)
    health_mgr: HealthManager = AsyncMock(spec=HealthManager)
    health_mgr.is_locked = lambda _d: False
    health_mgr.is_half_open = lambda d: d == "example.com"
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                delay_between_products=0.0,
                health_mgr=health_mgr,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    assert stub.calls == 1  # the half-open probe was sent
    health_mgr.record_success.assert_awaited_once_with("example.com")  # type: ignore[attr-defined]
    # The #20 guarantees still hold: no persist, no currency rewrite.
    p = await repo.get_product(pid)
    assert p is not None
    assert p.current_price != Decimal("80")
    assert p.currency == "EUR"


@pytest.mark.asyncio
async def test_scheduler_awaiting_confirmation_resets_error_counters(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A held, parsed price is a successful scrape even before persistence."""
    repo, pid = repo_with_product
    for price in (100, 102, 98, 105, 100):
        await repo.add_price_history(pid, Decimal(str(price)))
    await repo.record_failure(pid, reason="listing_gone", detail="HTTP 404")
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("50"), currency="EUR"))
    registry = ScraperRegistry()
    registry.register(stub)
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)

    product = await repo.get_product(pid)
    assert product is not None
    assert product.consecutive_errors == 0
    assert product.gone_streak == 0
    assert product.pending_read_price == Decimal("50")


@pytest.mark.asyncio
async def test_scheduler_currency_none_persists_normally(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Guard #20 must not fire when the scraper reports no currency."""
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency=None))
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
        await scheduler._check_product(pid, collector=NoticeCollector())
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
async def test_scheduler_price_none_keeps_scraper_error_detail(
    repo_with_product: tuple[Repository, int],
) -> None:
    """The scraper's ``error`` message must reach ``record_failure`` as ``detail``.

    When no price is read, the failure must keep the scraper's own
    explanation (e.g. "Impossibile caricare la pagina Amazon") alongside
    ``reason="price_none"``, so a suspended product can be diagnosed from
    the database. ``reason`` must stay ``"price_none"`` — only
    ``detail`` is new.
    """
    repo, pid = repo_with_product
    stub = _StubScraper(
        ProductInfo(name="Widget", price=None, error="Impossibile caricare la pagina Amazon")
    )
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    spy = AsyncMock(wraps=repo.record_failure)
    repo.record_failure = spy
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
    spy.assert_awaited_once_with(
        pid, reason="price_none", detail="Impossibile caricare la pagina Amazon"
    )
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 1
    assert p.last_error == "price_none: Impossibile caricare la pagina Amazon"


@pytest.mark.asyncio
async def test_scheduler_price_none_without_scraper_error_still_records_reason(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A scraper that sets no ``error`` message keeps today's behaviour: bare reason."""
    repo, pid = repo_with_product
    stub = _StubScraper(ProductInfo(name="Widget", price=None, error=None))
    registry = ScraperRegistry()
    registry.register(stub)
    notifier = AsyncMock()
    spy = AsyncMock(wraps=repo.record_failure)
    repo.record_failure = spy
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
    spy.assert_awaited_once_with(pid, reason="price_none", detail=None)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 1
    assert p.last_error == "price_none"


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
# Shared fixtures: scheduler_factory + sample_products
# (defined in tests/integration/conftest.py and shared across integration tests)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Domain-lock tests: skip-on-locked + half-open single probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_skips_locked_domain(
    scheduler_factory: object,
    sample_products: list[ProductRecord],
) -> None:
    health_mgr: HealthManager = AsyncMock(spec=HealthManager)
    health_mgr.is_locked = lambda d: d == "shop-a.com"
    health_mgr.is_half_open = lambda d: False

    scheduler: Scheduler = scheduler_factory(health_mgr=health_mgr)
    products = [p for p in sample_products if "shop-a.com" in p.url]
    scrape_calls: list[str] = []
    scheduler._scrape_one = AsyncMock(
        side_effect=lambda p, *, collector: scrape_calls.append(p.url)
    )

    await scheduler._run_tick(products, collector=NoticeCollector())

    assert scrape_calls == []  # all shop-a products skipped


@pytest.mark.asyncio
async def test_scheduler_half_open_sends_only_one_probe(
    scheduler_factory: object,
    sample_products: list[ProductRecord],
) -> None:
    health_mgr: HealthManager = AsyncMock(spec=HealthManager)
    health_mgr.is_locked = lambda d: False
    half_open_for: set[str] = {"shop-a.com"}
    health_mgr.is_half_open = lambda d: d in half_open_for

    scheduler: Scheduler = scheduler_factory(health_mgr=health_mgr)
    shop_a_products = [p for p in sample_products if "shop-a.com" in p.url]
    assert len(shop_a_products) >= 2  # ensure multiple products on same domain

    calls: list[str] = []
    scheduler._scrape_one = AsyncMock(side_effect=lambda p, *, collector: calls.append(p.url))
    await scheduler._run_tick(shop_a_products, collector=NoticeCollector())

    assert len(calls) == 1  # only one probe per half-open domain per tick


# ---------------------------------------------------------------------------
# Prometheus metric emission from Scheduler
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

    await scheduler._run_tick(sample_products[:1], collector=NoticeCollector())

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
    await scheduler._run_tick(sample_products[:3], collector=NoticeCollector())
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


@pytest.mark.asyncio
async def test_check_user_products_for_user_honors_delay_override(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Pull-mode caller can override ``delay_between_products`` to 0 for fast
    interactive batches without changing the gentle 5s default used by the
    periodic job."""
    import time  # noqa: PLC0415

    repo, pid = repo_with_product
    # Second product so the for-loop actually sleeps between iterations.
    await repo.add_product(
        user_id=1,
        url="https://example.com/p/2",
        name="Gadget",
        domain="example.com",
        initial_price=Decimal("50"),
        currency="EUR",
    )
    stub = _StubScraper(ProductInfo(name="X", price=Decimal("40"), currency="EUR"))
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
                # Default is gentle (5s). Test guards that the override actually
                # short-circuits — without it this test would take >5s.
                delay_between_products=5.0,
            )
        )
        t0 = time.monotonic()
        results = await scheduler.check_user_products_for_user(
            user_id=1, delay_between_products=0.0
        )
        elapsed = time.monotonic() - t0
    assert len(results) == 2
    # 2 products with delay=0 must complete in well under the deps default.
    assert elapsed < 2.0, f"override ignored: elapsed={elapsed:.2f}s with delay=0"


# ── Auto-disable on max consecutive errors (v0.1.9) ─────────────────


@pytest.mark.asyncio
async def test_product_auto_disabled_after_max_consecutive_errors(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Push mode: once a product accumulates ``max_consecutive_errors`` failures,
    the scheduler must keep it active with a non-suspension pre-warning before
    the threshold, then (1) deactivate it, (2) push one suspension notice, and
    (3) stop retrying it on subsequent ticks (because
    ``list_products_for_user(only_active=True)`` filters it out).
    """
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
                max_consecutive_errors=2,
                delay_between_products=0.0,
            )
        )
        # Tick 1: increment 1 → still active. With max=2 this is the
        # half-threshold pre-warning, never a suspension.
        await scheduler.run_check_for_user(user_id=1)
        p_after_1 = await repo.get_product(pid)
        assert p_after_1 is not None
        assert p_after_1.consecutive_errors == 1
        assert p_after_1.is_active is True
        assert notifier.await_count == 1
        first_payloads = [call.kwargs["payload"] for call in notifier.await_args_list]
        assert all(
            payload is not None and payload["event"] != "suspended" for payload in first_payloads
        )
        assert first_payloads[0]["event"] == "warning"
        # Tick 2: increment 2 → hits threshold → deactivate + notify
        await scheduler.run_check_for_user(user_id=1)
        p_after_2 = await repo.get_product(pid)
        assert p_after_2 is not None
        assert p_after_2.consecutive_errors == 2
        assert p_after_2.is_active is False
        assert notifier.await_count == 2
        # The notice is operational, has no product owner, and carries only the
        # closed group payload that the notifier/digest consumers share.
        call_args = notifier.await_args
        assert call_args is not None
        sent_user_id, sent_message = call_args.args
        assert sent_user_id == 1
        assert "Site unreachable" in sent_message
        assert "Error: <code>http_error: simulated network failure</code>" in sent_message
        assert call_args.kwargs["product_id"] is None
        payload = call_args.kwargs["payload"]
        assert payload is not None
        assert payload["kind"] == "operational"
        assert payload["event"] == "suspended"
        assert payload["product_ids"] == [pid]
        assert payload["products"][0]["id"] == pid
        assert len(payload["buttons"]) == 2
        assert "product_id" not in payload
        # Tick 3: only_active filter hides the product → no new scrape, no new notify
        await scheduler.run_check_for_user(user_id=1)
        p_after_3 = await repo.get_product(pid)
        assert p_after_3 is not None
        assert p_after_3.consecutive_errors == 2  # unchanged
        assert notifier.await_count == 2  # warning + suspension, still exactly once each


@pytest.mark.asyncio
async def test_check_user_products_for_user_marks_disabled_on_threshold(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Pull mode: when a product is auto-disabled mid-batch, the returned
    :class:`CheckResult` must carry ``disabled=True`` so the interactive
    handler can render a visual cue in the summary. The pre-warning and the
    later suspension are both delivered as distinct operational events.
    """
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
                max_consecutive_errors=2,
                delay_between_products=0.0,
            )
        )
        results_1 = await scheduler.check_user_products_for_user(user_id=1)
        assert len(results_1) == 1
        assert results_1[0].alert is None
        assert results_1[0].disabled is False  # not yet at threshold
        assert notifier.await_count == 1
        first_payloads = [call.kwargs["payload"] for call in notifier.await_args_list]
        assert all(
            payload is not None and payload["event"] != "suspended" for payload in first_payloads
        )
        assert first_payloads[0]["event"] == "warning"

        results_2 = await scheduler.check_user_products_for_user(user_id=1)
        assert len(results_2) == 1
        assert results_2[0].alert is None
        assert results_2[0].disabled is True  # hit threshold this tick
        assert notifier.await_count == 2
        suspension_payloads = [
            call.kwargs["payload"]
            for call in notifier.await_args_list
            if call.kwargs["payload"] is not None and call.kwargs["payload"]["event"] == "suspended"
        ]
        assert len(suspension_payloads) == 1

    p = await repo.get_product(pid)
    assert p is not None
    assert p.is_active is False
    assert p.consecutive_errors == 2


@pytest.mark.asyncio
async def test_default_error_threshold_warns_at_half_and_suspends_at_end(
    repo_with_product: tuple[Repository, int],
) -> None:
    """The production threshold warns after five failures and suspends after ten."""
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
                delay_between_products=0.0,
            )
        )

        for _ in range(4):
            await scheduler.run_check_for_user(user_id=1)
        assert notifier.await_count == 0

        await scheduler.run_check_for_user(user_id=1)
        p_after_5 = await repo.get_product(pid)
        assert p_after_5 is not None
        assert p_after_5.is_active is True
        assert p_after_5.consecutive_errors == 5
        assert notifier.await_count == 1
        warning_call = notifier.await_args
        assert warning_call is not None
        warning_payload = warning_call.kwargs["payload"]
        assert warning_payload is not None
        assert warning_payload["kind"] == "operational"
        assert warning_payload["event"] == "warning"
        assert warning_payload["error_count"] == 5
        assert warning_payload["max_errors"] == 10

        for _ in range(4):
            await scheduler.run_check_for_user(user_id=1)
        p_after_9 = await repo.get_product(pid)
        assert p_after_9 is not None
        assert p_after_9.is_active is True
        assert p_after_9.consecutive_errors == 9
        assert notifier.await_count == 1

        await scheduler.run_check_for_user(user_id=1)
        p_after_10 = await repo.get_product(pid)
        assert p_after_10 is not None
        assert p_after_10.is_active is False
        assert p_after_10.consecutive_errors == 10
        suspension_call = notifier.await_args
        assert suspension_call is not None
        suspension_payload = suspension_call.kwargs["payload"]
        assert suspension_payload is not None
        assert suspension_payload["kind"] == "operational"
        assert suspension_payload["event"] == "suspended"
        assert suspension_payload["error_count"] == 10
        assert suspension_payload["max_errors"] == 10


@pytest.mark.asyncio
async def test_failure_event_in_empty_collector_is_flushed(
    repo_with_product: tuple[Repository, int],
) -> None:
    """An explicit, initially empty collector retains the failure event for flush."""
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
                max_consecutive_errors=1,
            )
        )
        product = await repo.get_product(pid)
        assert product is not None
        collector = NoticeCollector()

        disabled = await scheduler._record_failure_and_maybe_disable(
            product,
            scraper_name="stub",
            domain="example.com",
            reason="parse_error",
            detail="broken listing",
            collector=collector,
        )

        assert disabled is True
        assert len(collector) == 1
        await scheduler._flush_notices(collector)

    notifier.assert_awaited_once()
    call_args = notifier.await_args
    assert call_args is not None
    payload = call_args.kwargs["payload"]
    assert payload is not None
    assert payload["product_ids"] == [pid]


@pytest.mark.asyncio
async def test_five_products_same_domain_emit_one_operational_notice(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, first_id = repo_with_product
    for number in range(2, 6):
        await repo.add_product(
            user_id=1,
            url=f"https://shop.example.com/p/{number}",
            name=f"Widget {number}",
            domain="example.com",
            initial_price=Decimal("100"),
            currency="EUR",
        )
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
                max_consecutive_errors=1,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    assert notifier.await_count == 1
    call_args = notifier.await_args
    assert call_args is not None
    payload = call_args.kwargs["payload"]
    assert payload is not None
    assert payload["product_ids"] == list(range(first_id, first_id + 5))


@pytest.mark.asyncio
async def test_warning_is_operational_and_exactly_once_per_episode(
    repo_with_product: tuple[Repository, int],
) -> None:
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
                max_consecutive_errors=4,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
        notifier.assert_not_awaited()
        await scheduler.run_check_for_user(user_id=1)
        await scheduler.run_check_for_user(user_id=1)
    assert notifier.await_count == 1
    call_args = notifier.await_args
    assert call_args is not None
    payload = call_args.kwargs["payload"]
    assert payload is not None
    assert payload["kind"] == "operational"
    assert payload["event"] == "warning"
    assert payload["product_ids"] == [pid]
    assert payload["buttons"] == []


@pytest.mark.asyncio
async def test_check_one_product_for_user_flushes_listing_gone_notice(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(_ScriptedScraper([ListingGone(status=404, url="https://example.com/p/1")]))
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                listing_gone_confirmations=1,
                delay_between_products=0.0,
            )
        )
        result = await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
    assert result.disabled is True
    assert result.reason == "listing_gone"
    assert notifier.await_count == 1
    call_args = notifier.await_args
    assert call_args is not None
    payload = call_args.kwargs["payload"]
    assert payload is not None
    assert payload["event"] == "suspended"
    assert payload["reason"] == "listing_gone"


@pytest.mark.asyncio
async def test_check_products_for_user_skips_foreign_and_missing_ids(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, own_id = repo_with_product
    await repo.ensure_user(user_id=2)
    foreign_id = await repo.add_product(
        user_id=2,
        url="https://example.net/p/1",
        name="Foreign",
        domain="example.net",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    registry = ScraperRegistry()
    registry.register(_StubScraper(ProductInfo(name="Widget", price=Decimal("80"), currency="EUR")))
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(repo=repo, registry=registry, client=client, notifier=AsyncMock())
        )
        results = await scheduler.check_products_for_user(
            product_ids=[999_999, foreign_id, own_id], user_id=1, delay_between_products=0.0
        )
    assert [result.product_id for result in results] == [own_id]


@pytest.mark.asyncio
async def test_notifier_failure_and_render_failure_do_not_abort_other_groups(
    repo_with_product: tuple[Repository, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _pid = repo_with_product
    await repo.add_product(
        user_id=1,
        url="https://other.example.net/p/2",
        name="Other",
        domain="example.net",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    registry = ScraperRegistry()
    registry.register(_RaisingScraper())
    notifier = AsyncMock(return_value=False)
    original_render = format_operational_notice

    def fail_first_group(group: NoticeGroup) -> str:
        if group.group_key == "example.com":
            raise RuntimeError("render failed")
        return original_render(group)

    monkeypatch.setattr("price_tracker.core.scheduler.format_operational_notice", fail_first_group)
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=1,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    assert notifier.await_count == 1


@pytest.mark.asyncio
async def test_flush_restores_locale_after_render_failure(
    repo_with_product: tuple[Repository, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from price_tracker.bot.messages import _, set_locale

    repo, _pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(_RaisingScraper())
    monkeypatch.setattr(
        "price_tracker.core.scheduler.format_operational_notice",
        lambda _group: (_ for _ in ()).throw(RuntimeError("render failed")),
    )
    set_locale("en")
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=1,
                delay_between_products=0.0,
                lang="it",
            )
        )
        # Call the flush DIRECTLY: run_check_for_user runs it inside a task,
        # and a task copies the context, so the caller's locale can never be
        # touched there — the assertion below would hold even without the reset.
        collector = NoticeCollector()
        collector.add(
            OperationalEvent(
                event="suspended",
                user_id=1,
                product_id=1,
                product_name="Widget",
                url="https://shop.example/p",
                group_key="shop.example",
                reason="listing_gone",
                detail=None,
                last_error=None,
                error_count=1,
                max_errors=1,
                last_price=None,
                currency=None,
                last_checked_at=None,
            )
        )
        await scheduler._flush_notices(collector)
    assert _("❌ Invalid ID.") == "❌ Invalid ID."


@pytest.mark.asyncio
async def test_listing_gone_suspends_after_three_confirmations(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(
        _ScriptedScraper(
            [
                ListingGone(status=404, url="https://example.com/p/1"),
                ListingGone(status=404, url="https://example.com/p/1"),
                ListingGone(status=404, url="https://example.com/p/1"),
            ]
        )
    )
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=10,
                listing_gone_confirmations=3,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
        await scheduler.run_check_for_user(user_id=1)
        after_two = await repo.get_product(pid)
        assert after_two is not None
        assert after_two.is_active is True
        assert after_two.gone_streak == 2

        await scheduler.run_check_for_user(user_id=1)

    after_three = await repo.get_product(pid)
    assert after_three is not None
    assert after_three.is_active is False
    assert after_three.last_error == "listing_gone: HTTP 404"
    assert after_three.consecutive_errors == 3
    assert after_three.suspension_kind == "automatic"
    assert after_three.suspension_reason == "listing_gone"
    notifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_listing_gone_streak_resets_on_other_failure(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(
        _ScriptedScraper(
            [
                ListingGone(status=404, url="https://example.com/p/1"),
                ListingGone(status=410, url="https://example.com/p/1"),
                ParseError("markup changed"),
                ListingGone(status=404, url="https://example.com/p/1"),
            ]
        )
    )
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                listing_gone_confirmations=3,
                delay_between_products=0.0,
            )
        )
        for _ in range(4):
            await scheduler.run_check_for_user(user_id=1)

    product = await repo.get_product(pid)
    assert product is not None
    assert product.is_active is True
    assert product.gone_streak == 1
    assert product.consecutive_errors == 4


@pytest.mark.asyncio
async def test_listing_gone_streak_resets_on_success(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(
        _ScriptedScraper(
            [
                ListingGone(status=404, url="https://example.com/p/1"),
                ListingGone(status=404, url="https://example.com/p/1"),
                ProductInfo(name="Widget", price=Decimal("99"), currency="EUR"),
            ]
        )
    )
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                listing_gone_confirmations=3,
                delay_between_products=0.0,
            )
        )
        for _ in range(3):
            await scheduler.run_check_for_user(user_id=1)

    product = await repo.get_product(pid)
    assert product is not None
    assert product.is_active is True
    assert product.gone_streak == 0
    assert product.consecutive_errors == 0


@pytest.mark.asyncio
async def test_http_status_404_from_raising_scraper_counts_as_listing_gone(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(_ScriptedScraper([_http_status_error(404)]))
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)

    product = await repo.get_product(pid)
    assert product is not None
    assert product.last_error is not None
    assert product.last_error.startswith("listing_gone")


@pytest.mark.asyncio
async def test_auto_suspension_writes_provenance(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(_RaisingScraper())
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=1,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)

    product = await repo.get_product(pid)
    assert product is not None
    assert product.is_active is False
    assert product.suspension_kind == "automatic"
    assert product.suspension_reason == "http_error"


@pytest.mark.asyncio
async def test_checkall_listing_gone_reaches_failure_recorder(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(_ScriptedScraper([ListingGone(status=410, url="https://example.com/p/1")]))
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                listing_gone_confirmations=3,
                delay_between_products=0.0,
            )
        )
        results = await scheduler.check_user_products_for_user(
            user_id=1, delay_between_products=0.0
        )

    product = await repo.get_product(pid)
    assert len(results) == 1
    assert results[0].disabled is False
    assert product is not None
    assert product.last_error == "listing_gone: HTTP 410"
    assert product.gone_streak == 1


# ── Anti-flap notification dedup (push path) ────────────────────────


class _SequenceScraper(AbstractScraper):
    """Scraper that returns a scripted sequence of prices, one per scrape call.

    Used to reproduce an oscillating ("flapping") price that repeatedly crosses
    the alert threshold on each downswing.
    """

    name = "sequence"
    priority = 100

    def __init__(self, prices: list[Decimal]) -> None:
        self._prices = prices
        self.calls = 0

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        price = self._prices[min(self.calls, len(self._prices) - 1)]
        self.calls += 1
        return ProductInfo(name="Widget", price=price, currency="EUR")


@pytest.mark.asyncio
async def test_scheduler_suppresses_duplicate_alert_on_flapping_price(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A price oscillating across the threshold must notify ONCE, not on every
    downswing.

    Reproduces a real case where a product flapping between its regular and sale
    price (423 ↔ 370.8, -12.3%) fired ~20 notifications because the push path had no anti-flap
    dedup. Initial price 100, threshold 10%; the scraped price flaps 80 ↔ 100
    over five ticks (downswings on ticks 1, 3, 5). Only the first downswing
    should reach the user.
    """
    repo, pid = repo_with_product
    stub = _SequenceScraper(
        [Decimal("80"), Decimal("100"), Decimal("80"), Decimal("100"), Decimal("80")]
    )
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
                notification_cooldown_hours=24,
            )
        )
        for _ in range(5):
            await scheduler.run_check_for_user(user_id=1)
    notifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_any_drop_notifies_on_small_decrease(
    repo_with_product: tuple[Repository, int],
) -> None:
    """An ``any_drop`` product (sentinel threshold) must notify on any decrease,
    even one far below a percentage threshold. Guards the regression where
    ``crosses_threshold`` ignored ``any_drop`` and these products never alerted.
    """
    repo, pid = repo_with_product
    await repo.set_threshold(pid, "any_drop", Decimal("0"))
    stub = _SequenceScraper([Decimal("99")])  # 1% drop from initial 100
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
                notification_cooldown_hours=24,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    notifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_renotifies_on_new_low(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A genuinely lower price (new low) overrides the cooldown and re-notifies
    — a better deal is worth interrupting for, even within the cooldown window.
    """
    repo, pid = repo_with_product
    stub = _SequenceScraper(
        [Decimal("80"), Decimal("100"), Decimal("70")]  # alert, recover, new low
    )
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
                notification_cooldown_hours=24,
            )
        )
        for _ in range(3):
            await scheduler.run_check_for_user(user_id=1)
    assert notifier.await_count == 2  # first drop (80) + new low (70)


@pytest.mark.asyncio
async def test_scheduler_renotifies_after_cooldown_elapsed(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A flapping price that re-crosses the threshold after the cooldown window
    has elapsed re-notifies once — the cooldown caps an oscillating price at one
    alert per window rather than silencing it forever.
    """
    repo, pid = repo_with_product
    stub = _SequenceScraper([Decimal("80"), Decimal("100"), Decimal("80")])
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
                notification_cooldown_hours=24,
            )
        )
        await scheduler.run_check_for_user(user_id=1)  # tick 1: 80 → alert
        notifier.assert_awaited_once()
        # Simulate the cooldown having elapsed (last alert 25h ago).
        await repo._conn.execute(  # noqa: SLF001
            "UPDATE products SET last_notified_at = datetime('now', '-25 hours') WHERE id = ?",
            (pid,),
        )
        await repo._conn.commit()  # noqa: SLF001
        await scheduler.run_check_for_user(user_id=1)  # tick 2: 100 → recover, no cross
        await scheduler.run_check_for_user(user_id=1)  # tick 3: 80 → re-cross, cooldown elapsed
    assert notifier.await_count == 2


class _CountingConnectErrorScraper(AbstractScraper):
    """Raises a non-block httpx error on every scrape and counts the attempts.

    Used to count HALF_OPEN probes: a non-block failure (timeout/connect error)
    leaves the domain HALF_OPEN, unlike a block (lock) or a success (close).
    """

    name = "counting-raising"
    priority = 100

    def __init__(self) -> None:
        self.calls = 0

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        self.calls += 1
        raise httpx.ConnectError("simulated timeout (non-block failure)")


@pytest.mark.asyncio
async def test_run_check_all_sends_single_half_open_probe_across_users() -> None:
    """Bug #17: a HALF_OPEN domain must receive exactly ONE probe per global
    sweep, even when multiple users track products on that domain.

    Three users each track one product on the same half-open domain; the probe
    fails with a non-block error (httpx.ConnectError) so the domain stays
    HALF_OPEN throughout the sweep (a block or a success would transition the
    state). Before the fix every per-user ``_run_tick`` created its own
    ``half_open_seen`` set, so ``run_check_all`` sent one probe per user.
    """
    from price_tracker.db.models import ProductRecord, UserRecord  # noqa: PLC0415

    def _make_product(pid: int, uid: int) -> ProductRecord:
        return ProductRecord(
            id=pid,
            user_id=uid,
            url=f"https://example.com/item/{pid}",
            name=f"Product {pid}",
            domain="example.com",
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

    users = [UserRecord(user_id=u, is_admin=False, is_active=True) for u in (1, 2, 3)]
    products_by_user = {u.user_id: [_make_product(100 + u.user_id, u.user_id)] for u in users}

    repo = AsyncMock()
    repo.list_active_users.return_value = users

    async def _list_products(*, user_id: int, only_active: bool = True) -> list[ProductRecord]:  # noqa: ARG001
        return products_by_user[user_id]

    repo.list_products_for_user.side_effect = _list_products

    async def _record_failure(
        pid: int,
        *,
        reason: str,
        detail: str | None = None,  # noqa: ARG001
    ) -> ProductRecord:
        return _make_product(pid, 1)  # consecutive_errors=0 → never auto-disabled

    repo.record_failure.side_effect = _record_failure

    health_mgr: HealthManager = AsyncMock(spec=HealthManager)
    health_mgr.is_locked = lambda _d: False
    health_mgr.is_half_open = lambda d: d == "example.com"

    scraper = _CountingConnectErrorScraper()
    registry = ScraperRegistry()
    registry.register(scraper)

    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                delay_between_products=0.0,
                health_mgr=health_mgr,
            )
        )
        await scheduler.run_check_all()

    assert scraper.calls == 1, (
        f"HALF_OPEN domain probed {scraper.calls} times in one global sweep (expected 1)"
    )


class _SelectiveScraper(AbstractScraper):
    """Raises an *unexpected* exception for one URL, returns a good price otherwise."""

    name = "selective"
    priority = 100

    def __init__(self, raise_on_url: str, good: ProductInfo) -> None:
        self._raise_on_url = raise_on_url
        self._good = good
        self.calls = 0

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        self.calls += 1
        if url == self._raise_on_url:
            # Not in the scheduler's caught set (Block/Parse/httpx/ValueError/KeyError).
            raise RuntimeError("unexpected scraper explosion")
        return self._good


@pytest.mark.asyncio
async def test_run_tick_survives_unexpected_scraper_exception(
    repo_with_product: tuple[Repository, int],
) -> None:
    """One product raising an unexpected exception must not abort the whole sweep.

    Regression for bug #2: a non-(Block/Parse/httpx/ValueError/KeyError) error
    (e.g. sqlite 'database is locked', RuntimeError) escaped ``_scrape_one`` and
    the un-guarded ``_run_tick`` loop, silently skipping every later product.
    """
    repo, pid1 = repo_with_product
    pid2 = await repo.add_product(
        user_id=1,
        url="https://example.com/p/2",
        name="Widget2",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    scraper = _SelectiveScraper(
        raise_on_url="https://example.com/p/1",
        good=ProductInfo(name="Widget2", price=Decimal("80"), currency="EUR"),
    )
    registry = ScraperRegistry()
    registry.register(scraper)
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)  # must NOT raise
    p2 = await repo.get_product(pid2)
    assert p2 is not None
    assert p2.current_price == Decimal("80")  # later product still processed
    p1 = await repo.get_product(pid1)
    assert p1 is not None
    assert p1.consecutive_errors == 1  # crashing product's failure recorded


@pytest.mark.asyncio
async def test_checkall_survives_unexpected_exception(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Pull-mode /checkall must not abort mid-list on an unexpected exception (#2)."""
    repo, _pid1 = repo_with_product
    pid2 = await repo.add_product(
        user_id=1,
        url="https://example.com/p/2",
        name="Widget2",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    scraper = _SelectiveScraper(
        raise_on_url="https://example.com/p/1",
        good=ProductInfo(name="Widget2", price=Decimal("80"), currency="EUR"),
    )
    registry = ScraperRegistry()
    registry.register(scraper)
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                max_consecutive_errors=10,
                delay_between_products=0.0,
            )
        )
        results = await scheduler.check_user_products_for_user(
            user_id=1, delay_between_products=0.0
        )
    assert len(results) == 2  # both products produced a result; sweep did not abort
    p2 = await repo.get_product(pid2)
    assert p2 is not None
    assert p2.current_price == Decimal("80")


class _CaptchaScraper(AbstractScraper):
    """Always raises a CAPTCHA BlockEvent, to drive a domain into quarantine."""

    name = "captcha"
    priority = 100

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        from price_tracker.core.exceptions import CaptchaDetected

        raise CaptchaDetected(marker="captcha-form", url=url)


@pytest.mark.asyncio
async def test_scheduler_notifies_once_on_quarantine_entry(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A domain crossing into LOCKED (T1 threshold = 3 blocks) pushes exactly one
    quarantine alert — on the CLOSED→LOCKED transition, not on every block."""
    repo, pid = repo_with_product
    # The notice is written in the recipient's language.
    await repo.set_user_language(1, "it")
    registry = ScraperRegistry()
    registry.register(_CaptchaScraper())
    notifier = AsyncMock()
    health_mgr = HealthManager(repo=repo)
    await health_mgr.load()
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
        product = await repo.get_product(pid)
        assert product is not None
        await scheduler._scrape_one(product, collector=NoticeCollector())
        assert notifier.await_count == 0  # block 1: still CLOSED
        await scheduler._scrape_one(product, collector=NoticeCollector())
        assert notifier.await_count == 0  # block 2: still CLOSED
        await scheduler._scrape_one(product, collector=NoticeCollector())
        assert notifier.await_count == 1  # block 3: CLOSED→LOCKED_T1 → notify once
        await scheduler._scrape_one(product, collector=NoticeCollector())
        assert notifier.await_count == 1  # still locked → no re-notify

    assert notifier.await_args is not None
    message = notifier.await_args.args[1]
    assert "example.com" in message
    assert "pausa automatica" in message.lower() or "quarantena" in message.lower()

    # last_error was persisted for /errori visibility (via the public projection).
    errored = await repo.list_products_with_errors(user_id=1)
    assert len(errored) == 1
    assert errored[0].last_error is not None
    assert "captcha-form" in errored[0].last_error.lower()


# ── Out-of-stock reads: a recognised sold-out page is state, not failure ──

_SOLD_OUT = ProductInfo(name="Widget", price=None, available=False, error="Prezzo non trovato")
_LAYOUT_BROKEN = ProductInfo(name="Widget", price=None, error="Prezzo non trovato")


async def _run_ticks(
    repo: Repository,
    outcomes: list[ProductInfo],
    *,
    notifier: AsyncMock,
    max_errors: int = 3,
) -> None:
    """Run one periodic check per scripted outcome against a single scheduler."""
    registry = ScraperRegistry()
    registry.register(_ScriptedScraper(list(outcomes)))
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=max_errors,
                delay_between_products=0.0,
            )
        )
        for _ in outcomes:
            await scheduler.run_check_for_user(user_id=1)


@pytest.mark.asyncio
async def test_out_of_stock_reads_never_suspend_the_product(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    notifier = AsyncMock()
    await _run_ticks(repo, [_SOLD_OUT] * 5, notifier=notifier, max_errors=3)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.is_active is True
    assert p.consecutive_errors == 0
    assert p.is_available is False
    assert p.current_price == Decimal("100")  # seeded at add, untouched
    assert await repo.get_price_history(pid) == []
    assert notifier.await_count == 0


@pytest.mark.asyncio
async def test_back_in_stock_after_out_of_stock_reads_notifies_exactly_once(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    notifier = AsyncMock()
    restocked = ProductInfo(name="Widget", price=Decimal("100"), currency="EUR", available=True)
    await _run_ticks(repo, [_SOLD_OUT] * 5 + [restocked, restocked], notifier=notifier)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.is_available is True
    assert p.current_price == Decimal("100")
    assert p.consecutive_errors == 0
    assert notifier.await_count == 1
    sent = notifier.await_args
    assert sent is not None
    assert sent.kwargs["product_id"] == pid
    assert sent.kwargs["payload"]["kind"] == "price"


@pytest.mark.asyncio
async def test_missing_price_without_out_of_stock_signal_still_suspends(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A layout change (price gone, availability not recognised) is a failure."""
    repo, pid = repo_with_product
    notifier = AsyncMock()
    await _run_ticks(repo, [_LAYOUT_BROKEN] * 3, notifier=notifier, max_errors=3)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.is_active is False
    assert p.consecutive_errors == 3
    assert p.is_available is True


@pytest.mark.asyncio
async def test_out_of_stock_read_resets_a_streak_of_price_failures(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    notifier = AsyncMock()
    await _run_ticks(repo, [_LAYOUT_BROKEN] * 5, notifier=notifier, max_errors=10)
    before = await repo.get_product(pid)
    assert before is not None
    assert before.consecutive_errors == 5
    notifier.reset_mock()
    await _run_ticks(repo, [_SOLD_OUT], notifier=notifier, max_errors=10)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 0
    assert p.is_active is True
    assert p.is_available is False
    assert notifier.await_count == 0


@pytest.mark.asyncio
async def test_sold_out_flag_with_a_price_writes_no_price_and_no_history(
    repo_with_product: tuple[Repository, int],
) -> None:
    """available=False is the out-of-stock state whatever the price on the page:
    a placeholder price is neither persisted nor added to the history."""
    repo, pid = repo_with_product
    notifier = AsyncMock()
    priced_sold_out = ProductInfo(
        name="Widget", price=Decimal("90"), currency="EUR", available=False
    )
    await _run_ticks(repo, [priced_sold_out], notifier=notifier)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.current_price == Decimal("100")  # the price seeded at add, untouched
    assert p.is_available is False
    assert await repo.get_price_history(pid) == []
    assert notifier.await_count == 0


@pytest.mark.asyncio
async def test_out_of_stock_read_writes_no_price(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    notifier = AsyncMock()
    await _run_ticks(repo, [_SOLD_OUT] * 2, notifier=notifier)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.current_price == Decimal("100")  # the price seeded at add, untouched
    assert await repo.get_price_history(pid) == []


@pytest.mark.asyncio
async def test_out_of_stock_read_clears_a_held_implausible_read(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    await repo.set_pending_read(pid, Decimal("5"), 1, 1)
    await _run_ticks(repo, [_SOLD_OUT], notifier=AsyncMock())
    p = await repo.get_product(pid)
    assert p is not None
    assert p.pending_read_price is None
    assert p.pending_read_count == 0
    assert p.pending_read_streak == 0


@pytest.mark.asyncio
async def test_out_of_stock_read_counts_as_a_domain_success(
    repo_with_product: tuple[Repository, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _pid = repo_with_product
    spy = AsyncMock()
    monkeypatch.setattr("price_tracker.core.scheduler.handle_success_in_pipeline", spy)
    await _run_ticks(repo, [_SOLD_OUT], notifier=AsyncMock())
    assert spy.await_count == 1
    assert spy.await_args is not None
    assert spy.await_args.kwargs["domain"] == "example.com"


@pytest.mark.asyncio
async def test_out_of_stock_then_layout_failures_still_suspend(
    repo_with_product: tuple[Repository, int],
) -> None:
    """A sold-out read must not buy immunity: later price_none failures count from zero
    and suspend, and availability stays False through them."""
    repo, pid = repo_with_product
    await _run_ticks(
        repo, [_SOLD_OUT, _LAYOUT_BROKEN, _LAYOUT_BROKEN], notifier=AsyncMock(), max_errors=2
    )
    p = await repo.get_product(pid)
    assert p is not None
    assert p.is_active is False
    assert p.consecutive_errors == 2
    assert p.is_available is False


@pytest.mark.asyncio
async def test_out_of_stock_then_restock_then_sold_out_then_restock_notifies_each_return(
    repo_with_product: tuple[Repository, int],
) -> None:
    """Flip-flop: one back-in-stock message per genuine return, none for the sold-out reads."""
    repo, pid = repo_with_product
    notifier = AsyncMock()
    back = ProductInfo(name="Widget", price=Decimal("100"), currency="EUR", available=True)
    await _run_ticks(repo, [_SOLD_OUT, back, _SOLD_OUT, _SOLD_OUT, back], notifier=notifier)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.is_active is True
    assert p.is_available is True
    assert p.consecutive_errors == 0
    assert notifier.await_count == 2


@pytest.mark.asyncio
async def test_out_of_stock_on_a_suspended_product_is_never_read(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    await repo.suspend_product(pid, reason="price_none")
    scraper = _ScriptedScraper([_SOLD_OUT])
    registry = ScraperRegistry()
    registry.register(scraper)
    notifier = AsyncMock()
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                delay_between_products=0.0,
            )
        )
        await scheduler.check_user_products_for_user(user_id=1)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.is_active is False
    assert p.is_available is True
    assert next(scraper._outcomes, None) is _SOLD_OUT  # never consumed


@pytest.mark.asyncio
async def test_out_of_stock_read_stamps_last_checked(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    before = await repo.get_product(pid)
    assert before is not None
    assert before.last_checked_at is None
    await _run_ticks(repo, [_SOLD_OUT], notifier=AsyncMock())
    p = await repo.get_product(pid)
    assert p is not None
    assert p.last_checked_at is not None


# -- suspension is reported once per product ---------------------------------


def _failure_scheduler(
    repo: Repository,
    notifier: AsyncMock,
    *,
    max_errors: int = 1,
    gone_confirmations: int = 3,
) -> tuple[Scheduler, httpx.AsyncClient]:
    registry = ScraperRegistry()
    registry.register(_RaisingScraper())
    client = httpx.AsyncClient()
    scheduler = Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=client,
            notifier=notifier,
            max_consecutive_errors=max_errors,
            listing_gone_confirmations=gone_confirmations,
            delay_between_products=0.0,
        )
    )
    return scheduler, client


def _suspended_payloads(notifier: AsyncMock) -> list[dict[str, Any]]:
    return [
        call.kwargs["payload"]
        for call in notifier.await_args_list
        if call.kwargs["payload"] is not None and call.kwargs["payload"]["event"] == "suspended"
    ]


async def _record_failure(
    scheduler: Scheduler,
    product: ProductRecord,
    collector: NoticeCollector,
    *,
    reason: str = "http_error",
) -> bool:
    return await scheduler._record_failure_and_maybe_disable(
        product,
        scraper_name="stub",
        domain="example.com",
        reason=reason,
        collector=collector,
    )


@pytest.mark.asyncio
async def test_concurrent_suspension_of_one_product_is_reported_once(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    notifier = AsyncMock()
    scheduler, client = _failure_scheduler(repo, notifier)
    async with client:
        product = await repo.get_product(pid)
        assert product is not None
        collectors = [NoticeCollector(), NoticeCollector()]
        results = await asyncio.gather(
            *(_record_failure(scheduler, product, collector) for collector in collectors)
        )
        for collector in collectors:
            await scheduler._flush_notices(collector)

    assert sorted(results) == [False, True]
    assert notifier.await_count == 1
    assert len(_suspended_payloads(notifier)) == 1
    stored = await repo.get_product(pid)
    assert stored is not None
    assert stored.is_active is False
    assert stored.suspension_reason == "http_error"


@pytest.mark.asyncio
async def test_failure_on_an_already_suspended_product_adds_no_event(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    notifier = AsyncMock()
    scheduler, client = _failure_scheduler(repo, notifier)
    async with client:
        product = await repo.get_product(pid)
        assert product is not None
        first = NoticeCollector()
        assert await _record_failure(scheduler, product, first) is True
        second = NoticeCollector()
        assert await _record_failure(scheduler, product, second) is False
    assert len(first) == 1
    assert len(second) == 0


@pytest.mark.asyncio
async def test_overlapping_sweeps_report_each_suspension_once(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, first_id = repo_with_product
    ids = [first_id]
    for number in (2, 3):
        ids.append(
            await repo.add_product(
                user_id=1,
                url=f"https://example.com/p/{number}",
                name=f"Widget {number}",
                domain="example.com",
                initial_price=Decimal("100"),
                currency="EUR",
            )
        )
    notifier = AsyncMock()
    scheduler, client = _failure_scheduler(repo, notifier)
    async with client:
        await asyncio.gather(
            scheduler.run_check_for_user(user_id=1),
            scheduler.check_user_products_for_user(user_id=1),
        )

    reported = [pid for payload in _suspended_payloads(notifier) for pid in payload["product_ids"]]
    assert len(reported) == len(set(reported))
    assert set(reported) == set(ids)


async def _concurrent_failures(calls: int, *, listing_gone: bool) -> tuple[list[bool], int, int]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
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
        notifier = AsyncMock()
        scheduler, client = _failure_scheduler(
            repo,
            notifier,
            max_errors=1 if not listing_gone else 10,
            gone_confirmations=1,
        )
        async with client:
            product = await repo.get_product(pid)
            assert product is not None
            collectors = [NoticeCollector() for _ in range(calls)]
            reason = "listing_gone" if listing_gone else "http_error"
            results = await asyncio.gather(
                *(
                    _record_failure(scheduler, product, collector, reason=reason)
                    for collector in collectors
                )
            )
            events = sum(len(collector) for collector in collectors)
            for collector in collectors:
                await scheduler._flush_notices(collector)
        return list(results), events, len(_suspended_payloads(notifier))
    finally:
        await conn.close()


@settings(max_examples=25, deadline=None)
@given(calls=st.integers(min_value=1, max_value=6), listing_gone=st.booleans())
def test_any_number_of_concurrent_failures_suspends_and_reports_once(
    calls: int, listing_gone: bool
) -> None:
    results, events, notices = asyncio.run(_concurrent_failures(calls, listing_gone=listing_gone))
    assert sum(results) == 1
    assert events == 1
    assert notices == 1


@pytest.mark.asyncio
async def test_failure_on_a_manually_paused_product_adds_no_event(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    notifier = AsyncMock()
    scheduler, client = _failure_scheduler(repo, notifier)
    async with client:
        product = await repo.get_product(pid)
        assert product is not None
        await repo.pause_product(pid)
        collector = NoticeCollector()
        assert await _record_failure(scheduler, product, collector) is False
    assert len(collector) == 0
    stored = await repo.get_product(pid)
    assert stored is not None
    assert stored.suspension_kind == "manual"


@pytest.mark.asyncio
async def test_failure_on_a_pause_without_kind_adds_no_event(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    notifier = AsyncMock()
    scheduler, client = _failure_scheduler(repo, notifier)
    async with client:
        product = await repo.get_product(pid)
        assert product is not None
        await repo._conn.execute(
            "UPDATE products SET is_active = 0, suspension_kind = NULL WHERE id = ?", (pid,)
        )
        await repo._conn.commit()
        collector = NoticeCollector()
        assert await _record_failure(scheduler, product, collector) is False
    assert len(collector) == 0
    stored = await repo.get_product(pid)
    assert stored is not None
    assert stored.suspension_kind is None


@pytest.mark.asyncio
async def test_failure_on_a_missing_product_adds_no_event(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    notifier = AsyncMock()
    scheduler, client = _failure_scheduler(repo, notifier)
    async with client:
        product = await repo.get_product(pid)
        assert product is not None
        assert await repo.delete_product(pid, user_id=1) is True
        collector = NoticeCollector()
        assert await _record_failure(scheduler, product, collector) is False
    assert len(collector) == 0


@pytest.mark.asyncio
async def test_failure_below_the_threshold_neither_suspends_nor_reports(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    notifier = AsyncMock()
    scheduler, client = _failure_scheduler(repo, notifier, max_errors=10)
    async with client:
        product = await repo.get_product(pid)
        assert product is not None
        collector = NoticeCollector()
        assert await _record_failure(scheduler, product, collector) is False
        await scheduler._flush_notices(collector)
    assert _suspended_payloads(notifier) == []
    stored = await repo.get_product(pid)
    assert stored is not None
    assert stored.is_active is True
