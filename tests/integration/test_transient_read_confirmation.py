"""Regression tests for transient bad reads reaching the alert path.

An isolated low reading between steady prices must not produce a price-drop
alert unless subsequent checks confirm the change.

The opposite failure mode is a sustained price rise rejected on every check:
a rejected read never enters price history and therefore cannot move the
median that rejects it. Consecutive agreeing reads must allow history to adapt.

Both are the same missing concept: an implausible reading must be confirmed by
a run of consecutive agreeing reads before the bot either trusts it or alerts
on it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from price_tracker.core.outlier import REQUIRED_CONFIRMATIONS
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")

# Synthetic prices exercise the confirmation thresholds without a recorded price series.
STEADY = Decimal("400.00")
GLITCH = Decimal("200.00")


class _ScriptedScraper(AbstractScraper):
    """Scraper returning a scripted sequence of readings, one per call."""

    name = "scripted"
    priority = 100

    def __init__(self, responses: list[ProductInfo]) -> None:
        self._responses = responses
        self.calls = 0

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        info = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return info


def _reading(price: Decimal, *, condition: str | None = None) -> ProductInfo:
    return ProductInfo(name="Widget", price=price, currency="EUR", condition=condition)


@pytest_asyncio.fixture
async def repo_with_history() -> AsyncIterator[tuple[Repository, int]]:
    """A product with a long, flat price history at STEADY."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    pid = await repo.add_product(
        user_id=1,
        url="https://www.amazon.it/dp/B0EXAMPLE1/?th=1",
        name="Example Building Set",
        domain="amazon.it",
        initial_price=STEADY,
        currency="EUR",
    )
    await repo.update_price(pid, STEADY)
    for _ in range(30):
        await repo.add_price_history(pid, STEADY)
    try:
        yield repo, pid
    finally:
        await conn.close()


async def _run(repo: Repository, scraper: AbstractScraper, notifier: AsyncMock, times: int) -> None:
    registry = ScraperRegistry()
    registry.register(scraper)
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
        for _ in range(times):
            await scheduler.run_check_for_user(user_id=1)


@pytest.mark.asyncio
async def test_single_transient_drop_does_not_alert(
    repo_with_history: tuple[Repository, int],
) -> None:
    """A steady price with one isolated glitch must not notify the user.

    The unconfirmed reading must not become the product's current price.
    """
    repo, pid = repo_with_history
    scraper = _ScriptedScraper([_reading(GLITCH), _reading(STEADY)])
    notifier = AsyncMock()

    await _run(repo, scraper, notifier, times=2)

    notifier.assert_not_awaited()
    product = await repo.get_product(pid)
    assert product is not None
    assert product.current_price == STEADY


@pytest.mark.asyncio
async def test_repeated_but_non_consecutive_glitch_never_alerts(
    repo_with_history: tuple[Repository, int],
) -> None:
    """Repeated glitches separated by good reads must remain unconfirmed."""
    repo, pid = repo_with_history
    scraper = _ScriptedScraper(
        [
            _reading(GLITCH),
            _reading(STEADY),
            _reading(GLITCH),
            _reading(STEADY),
            _reading(GLITCH),
            _reading(STEADY),
        ]
    )
    notifier = AsyncMock()

    await _run(repo, scraper, notifier, times=6)

    notifier.assert_not_awaited()
    product = await repo.get_product(pid)
    assert product is not None
    assert product.current_price == STEADY


@pytest.mark.asyncio
async def test_confirmed_real_drop_still_alerts(
    repo_with_history: tuple[Repository, int],
) -> None:
    """A genuine deep discount persists across checks, so it must still alert.

    Confirmation delays a real deep discount by a couple of check intervals; it
    must never suppress it.
    """
    repo, pid = repo_with_history
    scraper = _ScriptedScraper([_reading(GLITCH)])
    notifier = AsyncMock()

    await _run(repo, scraper, notifier, times=REQUIRED_CONFIRMATIONS)

    notifier.assert_awaited_once()
    assert notifier.await_args is not None
    message = notifier.await_args.args[1]
    assert "Price drop!" in message
    product = await repo.get_product(pid)
    assert product is not None
    assert product.current_price == GLITCH


@pytest.mark.asyncio
async def test_ordinary_drop_alerts_immediately(
    repo_with_history: tuple[Repository, int],
) -> None:
    """A plausible drop must not pay the confirmation latency."""
    repo, pid = repo_with_history
    modest = Decimal("340.00")  # -15%: crosses the 10% threshold, stays plausible
    scraper = _ScriptedScraper([_reading(modest)])
    notifier = AsyncMock()

    await _run(repo, scraper, notifier, times=1)

    notifier.assert_awaited_once()
    product = await repo.get_product(pid)
    assert product is not None
    assert product.current_price == modest


@pytest.mark.asyncio
async def test_sustained_price_rise_is_eventually_accepted(
    repo_with_history: tuple[Repository, int],
) -> None:
    """A sustained price rise must not be rejected forever.

    Synthetic history is flat at 20.00 and then rises to 70.00 (3.5x the
    median). The first read is implausible and held back, but once enough
    agreeing reads arrive it must be accepted so history can adapt — otherwise
    every future check rejects the same value against a median that can never
    move.
    """
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    try:
        await repo.ensure_user(user_id=1)
        pid = await repo.add_product(
            user_id=1,
            url="https://example.com/p/synthetic-rise",
            name="Synthetic Widget",
            domain="example.com",
            initial_price=Decimal("20.00"),
            currency="EUR",
        )
        await repo.update_price(pid, Decimal("20.00"))
        for _ in range(50):
            await repo.add_price_history(pid, Decimal("20.00"))

        scraper = _ScriptedScraper([_reading(Decimal("70.00"))])
        notifier = AsyncMock()
        await _run(repo, scraper, notifier, times=3)

        product = await repo.get_product(pid)
        assert product is not None
        assert product.current_price == Decimal("70.00")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_confirmations_must_be_consecutive(
    repo_with_history: tuple[Repository, int],
) -> None:
    """Garbage between two suspicious reads breaks the run; it must not be ignored.

    Readings absurd enough to be discarded outright used to leave the held-read
    state untouched, so a suspicious price interleaved with garbage still
    accumulated "consecutive" confirmations it never actually had.
    """
    repo, pid = repo_with_history
    absurd = Decimal("15000.00")  # 37.5x the median: discarded, not held
    scraper = _ScriptedScraper(
        [
            _reading(GLITCH),
            _reading(absurd),
            _reading(GLITCH),
            _reading(absurd),
            _reading(GLITCH),
        ]
    )
    notifier = AsyncMock()

    await _run(repo, scraper, notifier, times=5)

    notifier.assert_not_awaited()
    product = await repo.get_product(pid)
    assert product is not None
    assert product.current_price == STEADY


@pytest.mark.asyncio
async def test_failed_scrape_breaks_the_confirmation_run(
    repo_with_history: tuple[Repository, int],
) -> None:
    """A check that produced no price is not evidence for the previous one.

    The confirmation run has to be consecutive readings of the *same* claim. A
    scrape that failed to find a price tells us nothing, so it must reset the
    run rather than let two sightings an outage apart count as three.
    """
    repo, pid = repo_with_history
    scraper = _ScriptedScraper(
        [
            _reading(GLITCH),
            ProductInfo(name="Widget", price=None, error="price not found"),
            _reading(GLITCH),
            _reading(GLITCH),
        ]
    )
    notifier = AsyncMock()

    await _run(repo, scraper, notifier, times=4)

    notifier.assert_not_awaited()
    product = await repo.get_product(pid)
    assert product is not None
    assert product.current_price == STEADY


@pytest.mark.asyncio
async def test_absurd_readings_do_not_silently_freeze_the_product(
    repo_with_history: tuple[Repository, int],
) -> None:
    """Unusable readings must surface, not leave the product quietly stale.

    A price off by a scale-error magnitude is never trusted — no confirmation
    count should make the bot believe it. But discarding it in silence forever
    is the same trap as the outlier deadlock: the product keeps reporting a
    stale price and looks healthy. It has to end up in /errori instead.
    """
    repo, pid = repo_with_history
    scraper = _ScriptedScraper([_reading(Decimal("15000.00"))])
    notifier = AsyncMock()

    await _run(repo, scraper, notifier, times=3)

    product = await repo.get_product(pid)
    assert product is not None
    assert product.current_price == STEADY, "garbage must never be accepted as the price"
    errors = await repo.list_products_with_errors(user_id=1)
    assert [e.id for e in errors] == [pid]


@pytest.mark.asyncio
async def test_volatile_new_price_cannot_wedge_forever(
    repo_with_history: tuple[Repository, int],
) -> None:
    """A genuinely repriced but jittery product must not be frozen forever.

    Confirmation requires consecutive reads to *agree*. A product whose new
    price wobbles by more than the agreement tolerance on every check would
    never confirm, and — since held reads never enter history — the median that
    keeps rejecting it could never move. The gate has a bounded escape: after
    enough consecutive held reads it rebaselines on the latest one.
    """
    repo, pid = repo_with_history
    jittery = [
        _reading(Decimal(p))
        for p in ("150.00", "158.00", "146.00", "155.00", "148.00", "157.00", "149.00", "156.00")
    ]
    scraper = _ScriptedScraper(jittery)
    notifier = AsyncMock()

    await _run(repo, scraper, notifier, times=len(jittery))

    product = await repo.get_product(pid)
    assert product is not None
    assert product.current_price != STEADY, "product stayed frozen on the stale price"


@pytest.mark.asyncio
async def test_used_offer_is_not_tracked_when_user_wants_new(
    repo_with_history: tuple[Repository, int],
) -> None:
    """A used/warehouse buy-box price must not become the tracked price.

    The scrapers already detect the offer condition, but the scheduler ignored
    it, so a second-hand offer appearing in the buy-box was persisted as if it
    were the tracked new-product price — and could fire a price-drop alert.
    """
    repo, pid = repo_with_history
    await repo.set_product_preferences(pid, condition="new", seller=None)
    scraper = _ScriptedScraper([_reading(GLITCH, condition="used")])
    notifier = AsyncMock()

    await _run(repo, scraper, notifier, times=1)

    notifier.assert_not_awaited()
    product = await repo.get_product(pid)
    assert product is not None
    assert product.current_price == STEADY

    # Skipping the read must not be silent: a product whose pinned condition no
    # longer matches any offer would otherwise sit frozen on a stale price,
    # looking perfectly healthy while never updating again.
    errors = await repo.list_products_with_errors(user_id=1)
    assert [e.id for e in errors] == [pid]
    assert "used" in (errors[0].last_error or "")
