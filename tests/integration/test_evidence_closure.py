"""Regression witnesses for selected sold-out stock and declared currency evidence."""

from __future__ import annotations

import importlib
import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import pytest

from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.scrapers.evidence import offer_evidence, price_currency
from price_tracker.scrapers.secondary_evidence import dom_evidence
from price_tracker.scrapers.shopify import ShopifyScraper
from tests.integration.test_remaining_corrected import ALL_STORES

if TYPE_CHECKING:
    from price_tracker.core.scraper_base import ProductInfo

INVALID_DECLARATIONS = ["ZZZ", "garbage", "EUR garbage", "", " ", 1, False, [], {}]


async def _scrape_secondary(store: tuple[str, str, str, str], html: str) -> ProductInfo:
    module, class_name, fetch_name, url = store
    mod = importlib.import_module("price_tracker.scrapers." + module)
    response = (
        httpx.Response(200, text=html, request=httpx.Request("GET", url))
        if module == "aliexpress"
        else html
    )
    with patch.object(mod, fetch_name, new=AsyncMock(return_value=response)):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as client:
            result: ProductInfo = await getattr(mod, class_name)().scrape(url, client)
            return result


@pytest.mark.parametrize("declared", INVALID_DECLARATIONS)
def test_invalid_declaration_cannot_be_hidden_by_visible_currency(declared):
    assert price_currency("EUR 10", declared) == (None, True)
    assert dom_evidence("EUR 10", declared) == {"currency_ambiguous": True}


@pytest.mark.parametrize("declared", INVALID_DECLARATIONS)
@pytest.mark.parametrize("shape", ["alone", "first", "last"])
def test_invalid_eligible_offer_blocks_stock_and_other_price_fallback(declared, shape):
    bad = {"price": "EUR 10", "priceCurrency": declared, "availability": "OutOfStock"}
    good = {"price": "50.00", "priceCurrency": "EUR", "availability": "InStock"}
    offers = bad if shape == "alone" else [bad, good] if shape == "first" else [good, bad]
    result = offer_evidence(offers, strict=True)
    assert result is not None
    assert result["currency_ambiguous"] is True
    assert result["price"] is None
    assert result["available"] is None


@pytest.mark.parametrize("known_first", [False, True])
def test_unknown_offer_unit_cannot_be_compared_with_a_known_family_price(known_first):
    unknown = {"price": "10.00", "availability": "InStock"}
    known = {"price": "50.00", "priceCurrency": "EUR", "availability": "InStock"}
    offers = [known, unknown] if known_first else [unknown, known]
    result = offer_evidence(offers, strict=True)
    assert result is not None
    assert result["currency_ambiguous"] is True
    assert result["price"] is None
    assert result["available"] is None


def test_standalone_unknown_unit_stays_an_incomplete_quote_for_strategy_fallback():
    result = offer_evidence({"price": "10.00", "availability": "InStock"}, strict=True)
    assert result is not None
    assert result["price"] == Decimal("10.00")
    assert result["currency"] is None
    assert result["currency_ambiguous"] is False


@pytest.mark.parametrize("declared", ["EUR", "eur", " EUR ", "€"])
def test_exact_supported_declarations_remain_accepted_and_normalized(declared):
    assert price_currency("10.00", declared) == ("EUR", False)
    assert dom_evidence("10.00", declared) == {"price": Decimal("10.00"), "currency": "EUR"}
    result = offer_evidence(
        {"price": "10.00", "priceCurrency": declared, "availability": "InStock"}, strict=True
    )
    assert result is not None
    assert (result["price"], result["currency"], result["available"]) == (
        Decimal("10.00"),
        "EUR",
        True,
    )


def test_absent_declaration_allows_visible_currency_without_an_implicit_default():
    assert price_currency("EUR 10") == ("EUR", False)
    assert price_currency("10") == (None, False)
    assert dom_evidence("EUR 10") == {"price": Decimal("10"), "currency": "EUR"}


@pytest.mark.parametrize("declared", ["$", "EUR garbage", "USD"])
def test_optional_api_declaration_keeps_meaningful_ambiguous_or_conflicting_evidence(declared):
    assert price_currency("EUR 10", declared, optional_declaration=True) == (None, True)


def test_excluded_financing_offer_does_not_veto_a_valid_purchase_quote():
    result = offer_evidence(
        [
            {"price": "EUR 10/month", "priceCurrency": "ZZZ", "name": "Monthly installment"},
            {"price": "50.00", "priceCurrency": "EUR", "availability": "InStock"},
        ],
        strict=True,
    )
    assert result is not None
    assert (result["price"], result["currency"], result["available"]) == (
        Decimal("50.00"),
        "EUR",
        True,
    )


@pytest.mark.parametrize("store", ALL_STORES, ids=lambda store: store[0])
@pytest.mark.parametrize("mixed", [False, True])
async def test_secondary_scraper_rejects_masked_invalid_declared_currency(store, mixed):
    bad = {"price": "EUR 10", "priceCurrency": "ZZZ", "availability": "OutOfStock"}
    offers = (
        [bad, {"price": "50.00", "priceCurrency": "EUR", "availability": "InStock"}]
        if mixed
        else bad
    )
    node = {"@type": "Product", "url": store[3], "name": "Requested", "offers": offers}
    html = '<script type="application/ld+json">' + json.dumps(node) + "</script>"
    result = await _scrape_secondary(store, html)
    assert result.error == "currency_ambiguous"
    assert result.price is None
    assert result.stock_evidence is False


async def test_actual_dom_scraper_cannot_accept_invalid_local_currency():
    store = next(store for store in ALL_STORES if store[0] == "target")
    result = await _scrape_secondary(
        store,
        '<main><span data-test="product-price">EUR 10</span>'
        '<meta data-test="product-currency" content="ZZZ"></main>',
    )
    assert result.error == "currency_ambiguous"
    assert result.price is None
    assert result.stock_evidence is False


def _embedded_page(stock: bool | None, price: object) -> str:
    product = {
        "handle": "ring",
        "title": "Ring",
        "variants": [{"id": 1, "available": stock, "price": price, "price_currency": "EUR"}],
    }
    return (
        '<script id="ProductJson-main" type="application/json">' + json.dumps(product) + "</script>"
    )


@pytest.mark.parametrize("selected", [False, True], ids=["unselected", "selected"])
@pytest.mark.parametrize("stock", [False, True, None], ids=["sold-out", "in-stock", "unknown"])
@pytest.mark.parametrize("price", [None, 0, "not-a-price"], ids=["absent", "zero", "malformed"])
async def test_embedded_stock_without_price_reaches_durable_scheduler_state(selected, stock, price):
    url = "https://shop.example.com/products/ring" + ("?variant=1" if selected else "")
    scraper = ShopifyScraper()
    events: list[str] = []

    async def notify(
        user_id: int,
        text: str,
        *,
        product_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        del user_id, text, product_id
        assert payload is not None
        events.append(str(payload["event"]))
        return True

    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await apply_migrations(conn, Path("src/price_tracker/db/migrations"))
        repo = Repository(conn)
        await repo.ensure_user(user_id=1)
        pid = await repo.add_product(
            user_id=1,
            url=url,
            name="Ring",
            domain="shop.example.com",
            initial_price=Decimal("100"),
            currency="EUR",
        )
        if stock is not False:
            await repo.record_availability(pid, available=False)
            await repo.record_availability(pid, available=False)
            row = await repo.get_product(pid)
            assert row is not None
            await repo.acknowledge_status_notice(pid, row.notice_version)
        registry = ScraperRegistry()
        registry.register(scraper)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as client:
            scheduler = Scheduler(
                SchedulerDeps(
                    repo=repo,
                    registry=registry,
                    client=client,
                    notifier=notify,
                    delay_between_products=0,
                )
            )
            pages = [_embedded_page(stock, price)] * 2 + [_embedded_page(True, 10000)] * 2
            with (
                patch.object(scraper, "_try_json_api", AsyncMock(return_value=None)),
                patch.object(scraper, "_fetch_html", AsyncMock(side_effect=pages)),
            ):
                results = [
                    await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
                    for _ in range(2)
                ]
                row = await repo.get_product(pid)
                assert row is not None
                assert row.is_available is False
                assert row.sold_out_streak == 2
                assert row.current_price == Decimal("100")
                assert await repo.get_price_history(pid) == []
                if stock is False:
                    assert [result.reason for result in results] == ["out_of_stock", "out_of_stock"]
                    assert row.consecutive_errors == 0
                    assert events == ["unavailable"]
                else:
                    assert [result.reason for result in results] == ["price_none", "price_none"]
                    assert row.consecutive_errors == 2
                    assert events == []
                for _ in range(2):
                    await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
            row = await repo.get_product(pid)
            assert row is not None
            assert row.is_available is True
            assert row.consecutive_errors == 0
            assert events.count("restocked") == 1
