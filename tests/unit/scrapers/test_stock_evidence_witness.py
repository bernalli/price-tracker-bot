"""Offline characterization: absent availability after confirmed sold-out."""

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from price_tracker.core.http_client import build_client
from price_tracker.core.registry import ScraperRegistry, discover_builtin_scrapers
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from tests.unit.scrapers.test_nove25 import (
    _response,
    wire,  # noqa: F401
)


@pytest.mark.asyncio
async def test_unknown_availability_preserves_sold_out_without_restock(wire):  # noqa: F811
    responses, _ = wire
    url = "https://nove25.net/item"

    def page(availability: str | None, price: str = "100.00") -> str:
        offer = {"price": price, "priceCurrency": "EUR"}
        if availability:
            offer["availability"] = availability
        product = {"@type": "Product", "url": url, "name": "Ring", "offers": offer}
        return '<script type="application/ld+json">' + json.dumps(product) + "</script>"

    responses.extend(
        [
            _response("200 OK", page("https://schema.org/OutOfStock")),
            _response("200 OK", page("https://schema.org/OutOfStock")),
            _response("200 OK", page(None, "80.00")),
            _response("200 OK", page("https://schema.org/InStock", "80.00")),
            _response("200 OK", page("https://schema.org/InStock", "80.00")),
        ]
    )
    conn = await aiosqlite.connect(":memory:")
    try:
        conn.row_factory = aiosqlite.Row
        import price_tracker

        migrations = Path(price_tracker.__file__).parent / "db/migrations"
        await apply_migrations(conn, migrations)
        repo = Repository(conn)
        await repo.ensure_user(user_id=1)
        pid = await repo.add_product(
            user_id=1,
            url=url,
            name="Ring",
            domain="nove25.net",
            initial_price=Decimal("100"),
            currency="EUR",
        )
        registry = ScraperRegistry()
        discover_builtin_scrapers(registry)
        notifier = AsyncMock(return_value=True)
        async with build_client() as client:
            scheduler = Scheduler(
                SchedulerDeps(
                    repo=repo,
                    registry=registry,
                    client=client,
                    notifier=notifier,
                    delay_between_products=0,
                )
            )
            await scheduler.run_check_for_user(user_id=1)
            await scheduler.run_check_for_user(user_id=1)
            before = await repo.get_product(pid)
            assert before is not None
            assert before.is_available is False
            await scheduler.run_check_for_user(user_id=1)
            after_unknown = await repo.get_product(pid)
            assert after_unknown is not None
            assert after_unknown.is_available is False
            assert after_unknown.last_check_outcome == "availability_unknown"
            assert after_unknown.current_price == Decimal("80.00")
            assert [call.kwargs["payload"]["event"] for call in notifier.await_args_list] == [
                "unavailable"
            ]
            await scheduler.run_check_for_user(user_id=1)
            await scheduler.run_check_for_user(user_id=1)
        after = await repo.get_product(pid)
        events = [call.kwargs["payload"]["event"] for call in notifier.await_args_list]
        assert after is not None
        assert after.is_available is True
        assert events == ["unavailable", "restocked"]
    finally:
        await conn.close()
