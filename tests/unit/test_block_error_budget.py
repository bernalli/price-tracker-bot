"""A store that refuses the bot is a store-level condition, not a product failure.

The per-store circuit breaker pauses the whole store. A refused check must not
also spend the product's own error budget: the first product of a blocked store
was the probe of every retry, reached the limit and was suspended, and stayed
suspended after the store answered again.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx

from price_tracker.core.exceptions import CaptchaDetected, HTTPBlockStatus
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import CheckResult, Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from tests.unit.test_check_out_of_stock import URL, repo_with_product  # noqa: F401

if TYPE_CHECKING:
    from price_tracker.db.repository import Repository

Outcome = ProductInfo | Exception
MAX_ERRORS = 4


class _Scripted(AbstractScraper):
    name = "scripted"
    priority = 100

    def __init__(self, outcomes: list[Outcome]) -> None:
        self._outcomes = iter(outcomes)

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _run(
    repo: Repository, pid: int, outcomes: list[Outcome]
) -> tuple[list[CheckResult], AsyncMock]:
    registry = ScraperRegistry()
    registry.register(_Scripted(outcomes))
    notifier = AsyncMock(return_value=True)
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                max_consecutive_errors=MAX_ERRORS,
                delay_between_products=0.0,
            )
        )
        results = [
            await scheduler.check_one_product_for_user(product_id=pid, user_id=1) for _ in outcomes
        ]
    return results, notifier


def _refused() -> HTTPBlockStatus:
    return HTTPBlockStatus(status=403, url=URL)


async def test_repeated_blocks_never_suspend_the_product(repo_with_product):  # noqa: F811
    repo, pid = repo_with_product
    results, notifier = await _run(repo, pid, [_refused() for _ in range(MAX_ERRORS * 3)])
    row = await repo.get_product(pid)
    assert [r.reason for r in results] == ["block"] * (MAX_ERRORS * 3)
    assert not any(r.disabled for r in results)
    assert row.is_active is True
    assert row.suspension_kind is None
    assert row.consecutive_errors == 0
    events = [call.kwargs["payload"]["event"] for call in notifier.await_args_list]
    assert "suspended" not in events
    assert "warning" not in events


async def test_a_block_is_recorded_as_the_last_outcome_and_error(repo_with_product):  # noqa: F811
    repo, pid = repo_with_product
    await _run(repo, pid, [CaptchaDetected(marker="challenge", url=URL)])
    row = await repo.get_product(pid)
    assert row.last_check_outcome == "block"
    assert row.last_error is not None
    assert row.last_error.startswith("block")
    assert row.last_error_at is not None
    assert row.current_price is None or row.current_price == Decimal("100")


async def test_a_block_neither_spends_nor_restores_the_budget_of_real_failures(
    repo_with_product,  # noqa: F811
):
    repo, pid = repo_with_product
    unreadable = ProductInfo(error="no price on the page", stock_evidence=False)
    results, _notifier = await _run(
        repo, pid, [unreadable, _refused(), unreadable, _refused(), _refused()]
    )
    row = await repo.get_product(pid)
    assert [r.reason for r in results] == ["price_none", "block", "price_none", "block", "block"]
    assert row.consecutive_errors == 2
    assert row.is_active is True


async def test_real_failures_still_suspend_after_blocks(repo_with_product):  # noqa: F811
    repo, pid = repo_with_product
    unreadable = ProductInfo(error="no price on the page", stock_evidence=False)
    results, _notifier = await _run(repo, pid, [_refused(), *[unreadable] * MAX_ERRORS])
    row = await repo.get_product(pid)
    assert results[-1].disabled is True
    assert row.is_active is False
    assert row.suspension_reason == "price_none"


async def test_a_block_interrupts_a_missing_page_streak(repo_with_product):  # noqa: F811
    from price_tracker.core.exceptions import ListingGone

    repo, pid = repo_with_product
    gone = ListingGone(status=404, url=URL)
    await _run(repo, pid, [gone, _refused()])
    row = await repo.get_product(pid)
    assert row.gone_streak == 0
    assert row.is_active is True


# ── A refused product stays visible although it carries no counted error ──


async def test_refused_product_is_listed_in_the_errors_report(repo_with_product):  # noqa: F811
    repo, pid = repo_with_product
    await _run(repo, pid, [_refused()])
    rows = await repo.list_products_with_errors(user_id=1)
    assert [row.id for row in rows] == [pid]
    assert rows[0].consecutive_errors == 0
    assert rows[0].last_check_outcome == "block"


async def test_product_skipped_for_a_paused_store_is_listed(repo_with_product):  # noqa: F811
    repo, pid = repo_with_product
    for outcome in ("quarantined", "probe_skipped"):
        await repo.record_check_outcome(pid, reason=outcome)
        rows = await repo.list_products_with_errors(user_id=1)
        assert [row.last_check_outcome for row in rows] == [outcome]


async def test_other_outcomes_and_paused_products_are_not_listed(repo_with_product):  # noqa: F811
    repo, pid = repo_with_product
    for outcome in ("deferred", "awaiting_confirmation", "availability_unknown"):
        await repo.record_check_outcome(pid, reason=outcome)
        assert await repo.list_products_with_errors(user_id=1) == []
    await repo.record_check_outcome(pid, reason="block")
    await repo.pause_product(pid)
    assert await repo.list_products_with_errors(user_id=1) == []


async def test_errors_report_explains_a_refused_product_without_a_counter():
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock

    from price_tracker.bot.handlers.debug import errors_text
    from price_tracker.core.health import HealthManager
    from price_tracker.db.models import ProductErrorRow, ScraperHealth

    now = datetime.now(UTC)
    db = MagicMock()
    db.list_products_with_errors = AsyncMock(
        return_value=[
            ProductErrorRow(
                id=7,
                name="Runner",
                # The stored column is stale: the store is identified by the URL.
                url="https://www.shop-b.com/products/runner",
                domain="shop",
                consecutive_errors=0,
                last_error=None,
                last_error_at=None,
                last_check_outcome="quarantined",
            )
        ]
    )
    health = HealthManager(repo=MagicMock())
    health._records = {
        "shop-b.com": ScraperHealth(
            domain="shop-b.com",
            state="LOCKED_T3",
            consecutive_blocks=14,
            locked_until=now + timedelta(hours=20),
            last_block_at=now,
            last_block_reason="HTTP 403",
        )
    }
    text = await errors_text(db, health, 1)
    assert text is not None
    assert "⚠️0" not in text
    assert "🔒" in text
    assert "Check postponed while the site is temporarily restricted." in text
    assert "Checks for this site are temporarily paused." in text


def test_list_error_filter_counts_a_refused_active_product():
    from price_tracker.bot.handlers._cards import list_view

    def record(pid: int, **extra: object) -> dict[str, object]:
        base: dict[str, object] = {
            "id": pid,
            "url": f"https://shop.example/products/p{pid}",
            "name": f"P{pid}",
            "currency": "EUR",
            "current_price": "10",
            "is_active": 1,
            "is_available": 1,
            "consecutive_errors": 0,
            "threshold_type": "any_drop",
            "threshold_value": "0",
        }
        base.update(extra)
        return base

    records = [
        record(1),
        record(2, last_check_outcome="block"),
        record(3, last_check_outcome="quarantined"),
        record(4, consecutive_errors=2, last_check_outcome="price_none"),
        record(5, is_active=0, last_check_outcome="block"),
        record(6, last_check_outcome="deferred"),
    ]
    page = list_view(records, "e", 1, default_interval_minutes=60)
    assert [view.id for view in page.items] == [2, 3, 4]
    assert page.total == 3
