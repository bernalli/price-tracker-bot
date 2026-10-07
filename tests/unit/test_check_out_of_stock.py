"""An out-of-stock listing is a state, not a price read: ``reason`` and the /check wording."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from price_tracker.bot.handlers import monitoring
from price_tracker.bot.handlers.callbacks import _product
from price_tracker.bot.messages import set_locale
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import CheckResult, Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src/price_tracker/db/migrations"
URL = "https://example.com/p/1"
OUT_OF_STOCK_LINE = "📦 Out of stock - I will tell you when it is back."
NO_CHANGE_LINE = "📊 No significant change."


class _Scripted(AbstractScraper):
    name = "scripted"
    priority = 100

    def __init__(self, outcomes: list[ProductInfo]) -> None:
        self._outcomes = iter(outcomes)

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        return next(self._outcomes)


@pytest_asyncio.fixture
async def repo_with_product() -> AsyncIterator[tuple[Repository, int]]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    pid = await repo.add_product(
        user_id=1,
        url=URL,
        name="Kettle",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    try:
        yield repo, pid
    finally:
        await conn.close()


async def _pull(
    repo: Repository, pid: int, outcomes: list[ProductInfo], *, max_errors: int = 3
) -> list[CheckResult]:
    """Run one pull-mode check per scripted outcome and return every result."""
    registry = ScraperRegistry()
    registry.register(_Scripted(outcomes))
    notifier = AsyncMock()
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
        results = [
            await scheduler.check_one_product_for_user(product_id=pid, user_id=1) for _ in outcomes
        ]
    return results


_NO_PRICE = ProductInfo(name="Kettle", price=None, available=False, error="no price")
_WITH_PRICE = ProductInfo(name="Kettle", price=Decimal("10"), currency="EUR", available=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("info", [_NO_PRICE, _WITH_PRICE], ids=["no-price", "placeholder-price"])
async def test_sold_out_read_reports_out_of_stock_and_writes_no_price(
    repo_with_product: tuple[Repository, int], info: ProductInfo
) -> None:
    repo, pid = repo_with_product
    (result,) = await _pull(repo, pid, [info])
    assert result.reason == "out_of_stock"
    assert result.alert is None
    product = await repo.get_product(pid)
    assert product is not None
    assert product.is_available is False
    assert product.current_price == Decimal("100")
    assert await repo.get_price_history(pid) == []


@pytest.mark.asyncio
async def test_normal_read_has_no_reason(repo_with_product: tuple[Repository, int]) -> None:
    repo, pid = repo_with_product
    (result,) = await _pull(
        repo, pid, [ProductInfo(name="Kettle", price=Decimal("100"), currency="EUR")]
    )
    assert result.reason is None


@pytest.mark.asyncio
async def test_missing_price_without_the_sold_out_signal_is_still_a_failure(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    layout_broken = ProductInfo(name="Kettle", price=None, error="no price")
    results = await _pull(repo, pid, [layout_broken] * 2, max_errors=2)
    assert [r.reason for r in results] == [None, None]
    assert [r.disabled for r in results] == [False, True]
    product = await repo.get_product(pid)
    assert product is not None
    assert product.is_active is False


@pytest.mark.asyncio
async def test_back_in_stock_after_a_priced_sold_out_read_notifies_once(
    repo_with_product: tuple[Repository, int],
) -> None:
    repo, pid = repo_with_product
    registry = ScraperRegistry()
    registry.register(
        _Scripted(
            [
                _WITH_PRICE,
                ProductInfo(name="Kettle", price=Decimal("100"), currency="EUR", available=True),
            ]
        )
    )
    notifier = AsyncMock(return_value=True)
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
        await scheduler.run_check_for_user(user_id=1)
        await scheduler.run_check_for_user(user_id=1)
    notifier.assert_awaited_once()
    assert notifier.await_args is not None
    assert "stock" in notifier.await_args.args[1].lower()


def _check_env(reason: str | None) -> tuple[Any, Any, Any]:
    """A /check call whose scheduler reports ``reason`` and no alert."""
    row = {
        "id": 1,
        "user_id": 7,
        "name": "Kettle",
        "url": URL,
        "current_price": "50.00",
        "initial_price": "60.00",
        "is_active": 1,
        "currency": "EUR",
    }
    db = AsyncMock()
    db.is_user_allowed.return_value = True
    db.get_product.return_value = row
    db.get_product_for_user.return_value = row
    scheduler = MagicMock()
    scheduler.check_one_product_for_user = AsyncMock(
        return_value=CheckResult(product_id=1, user_id=7, reason=reason)
    )
    context = MagicMock()
    context.bot_data = {"db": db, "scheduler": scheduler, "config": SimpleNamespace()}
    context.args = ["1"]
    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 7
    update.effective_user.language_code = "en"
    update.message.reply_text = AsyncMock(return_value=placeholder)
    return update, context, placeholder


@pytest.mark.asyncio
async def test_check_command_says_out_of_stock_only_for_that_reason() -> None:
    update, context, placeholder = _check_env("out_of_stock")
    await monitoring.cmd_check(update, context)
    text = placeholder.edit_text.await_args.args[0]
    assert OUT_OF_STOCK_LINE in text
    assert NO_CHANGE_LINE not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "price_none", "block"])
async def test_check_command_keeps_the_no_change_line_otherwise(reason: str | None) -> None:
    update, context, placeholder = _check_env(reason)
    await monitoring.cmd_check(update, context)
    text = placeholder.edit_text.await_args.args[0]
    assert NO_CHANGE_LINE in text
    assert OUT_OF_STOCK_LINE not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "expected"), [("out_of_stock", True), (None, False)])
async def test_check_button_appends_the_line_only_for_out_of_stock(
    reason: str | None, expected: bool
) -> None:
    set_locale("en")
    update, context, _placeholder = _check_env(reason)
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    await _product.handle_check_button(query, context, context.bot_data["db"], 7, "check_1")
    text = query.edit_message_text.await_args.args[0]
    assert (OUT_OF_STOCK_LINE in text) is expected
