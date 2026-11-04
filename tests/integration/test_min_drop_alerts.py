"""A price alert needs a drop above the floor, whatever the product's rule."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.core.drop_floor import DEFAULT_DROP_FLOOR, DropFloor
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


class _PriceScraper(AbstractScraper):
    name = "fixed"
    priority = 100

    def __init__(self, price: str) -> None:
        self.price = Decimal(price)

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        return ProductInfo(name="AirPods", price=self.price, currency="EUR")


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


async def _check(
    repo: Repository,
    new_price: str,
    *,
    rule: str = "any_drop",
    value: str = "0",
    floor: DropFloor = DEFAULT_DROP_FLOOR,
) -> bool:
    pid = await repo.add_product(
        user_id=1,
        url="https://store.example/products/airpods",
        name="AirPods",
        domain="store.example",
        initial_price=Decimal("199.49"),
        currency="EUR",
        threshold_type=rule,
        threshold_value=Decimal(value),
    )
    registry = ScraperRegistry()
    registry.register(_PriceScraper(new_price))
    scheduler = Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=AsyncMock(),
            notifier=AsyncMock(),
            delay_between_products=0,
            drop_floor=floor,
        )
    )
    result = await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
    product = await repo.get_product(pid)
    assert product is not None
    assert product.current_price == Decimal(new_price)  # the read itself is always kept
    return result.alert is not None


async def test_airpods_drop_of_49_cents_does_not_alert(repo: Repository) -> None:
    assert not await _check(repo, "199.00")


async def test_airpods_real_drop_alerts(repo: Repository) -> None:
    assert await _check(repo, "189.00")


@pytest.mark.parametrize(
    ("rule", "value"), [("percentage", "0.1"), ("absolute", "0.10"), ("target", "199.10")]
)
async def test_every_rule_type_needs_the_floor(repo: Repository, rule: str, value: str) -> None:
    assert not await _check(repo, "199.00", rule=rule, value=value)


async def test_the_user_switching_the_floor_off_restores_todays_behaviour(
    repo: Repository,
) -> None:
    assert await repo.set_user_min_drop(1, "off")
    assert await _check(repo, "199.00")


@pytest.mark.parametrize(
    ("choice", "price", "expected"),
    [("5", "190.00", False), ("5", "189.00", True), ("1", "197.00", True)],
)
async def test_a_user_preset_replaces_the_default_percentage(
    repo: Repository, choice: str, price: str, expected: bool
) -> None:
    await repo.set_user_min_drop(1, choice)
    assert await _check(repo, price) is expected


async def test_the_preset_keeps_the_absolute_floor(repo: Repository) -> None:
    await repo.set_user_min_drop(1, "1")
    # 1 % of 199.49 is 1.99, so the absolute part is what is tested on a cheap item.
    pid = await repo.add_product(
        user_id=1,
        url="https://store.example/products/cable",
        name="Cable",
        domain="store.example",
        initial_price=Decimal("9.99"),
        currency="EUR",
        threshold_type="any_drop",
        threshold_value=Decimal(0),
    )
    registry = ScraperRegistry()
    registry.register(_PriceScraper("9.49"))
    scheduler = Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=AsyncMock(),
            notifier=AsyncMock(),
            delay_between_products=0,
            drop_floor=DEFAULT_DROP_FLOOR,
        )
    )
    result = await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
    assert result.alert is None  # 5 % but only 0.50


async def test_scheduler_deps_alert_on_every_drop_unless_configured(repo: Repository) -> None:
    deps = SchedulerDeps(
        repo=repo, registry=ScraperRegistry(), client=AsyncMock(), notifier=AsyncMock()
    )
    assert deps.drop_floor.is_off


async def test_a_stored_choice_round_trips_and_is_validated(repo: Repository) -> None:
    assert await repo.get_user_min_drop(1) is None
    for choice in ("off", "1", "2", "5", None):
        assert await repo.set_user_min_drop(1, choice)
        assert await repo.get_user_min_drop(1) == choice
    with pytest.raises(ValueError, match="min_drop"):
        await repo.set_user_min_drop(1, "3")
    assert not await repo.set_user_min_drop(99, "2")
    assert await repo.get_user_min_drop(99) is None
