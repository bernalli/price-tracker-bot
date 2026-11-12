"""Adversarial secondary evidence and import-to-scheduler money regressions."""

import importlib
import json
from decimal import Decimal
from pathlib import Path
from typing import TypedDict
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import pytest

from price_tracker.bot.handlers.product_io import cmd_import
from price_tracker.core.money import MAX_AMOUNT
from price_tracker.core.page_image import ImagePageHTML
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.scrapers.evidence import offer_evidence
from tests.integration.test_scheduler import _StubScraper
from tests.unit.scrapers.test_remaining_evidence_regressions import STORES
from tests.unit.test_product_io_csv_format import _context

ALL_STORES = [
    *STORES,
    (
        "aliexpress",
        "AliexpressScraper",
        "_fetch_aliexpress_html",
        "https://www.aliexpress.com/item/1.html",
    ),
]


def ld(value: object) -> str:
    return '<script type="application/ld+json">' + json.dumps(value) + "</script>"


ProductJSON = TypedDict(
    "ProductJSON", {"@type": str, "url": str, "name": str, "offers": dict[str, str]}
)


def product(url: str, *, price: str = "100", stock: str = "InStock") -> ProductJSON:
    return {
        "@type": "Product",
        "url": url,
        "name": "Requested",
        "offers": {
            "@type": "Offer",
            "url": url,
            "price": price,
            "priceCurrency": "GBP",
            "availability": stock,
        },
    }


async def scrape(
    store: tuple[str, str, str, str], html: str, final_url: str | None = None
) -> ProductInfo:
    module, cls, fetch, url = store
    mod = importlib.import_module("price_tracker.scrapers." + module)
    response = (
        httpx.Response(200, text=str(html), request=httpx.Request("GET", final_url or url))
        if module == "aliexpress"
        else html
    )
    with patch.object(mod, fetch, new=AsyncMock(return_value=response)):
        async with httpx.AsyncClient() as client:
            result = await getattr(mod, cls)().scrape(url, client)
            assert isinstance(result, ProductInfo)
            return result


@pytest.mark.parametrize("store", ALL_STORES, ids=lambda s: s[0])
async def test_requested_product_wins_over_foreign_sold_out_recommendation(store):
    result = await scrape(
        store,
        ld(
            [
                product("https://unrelated.invalid/other", price="7", stock="OutOfStock"),
                product(store[3]),
            ]
        ),
    )
    assert (result.price, result.currency, result.available) == (Decimal("100"), "GBP", True)


@pytest.mark.parametrize("store", ALL_STORES, ids=lambda s: s[0])
@pytest.mark.parametrize(
    "case", ["foreign_child", "foreign_product", "wrong_variant", "canonical", "redirect"]
)
async def test_foreign_identity_never_becomes_price_or_stock(store, case):
    node = product(store[3])
    html_prefix = ""
    final_url = store[3]
    if case == "foreign_child":
        node["offers"]["url"] = store[3] + "-other"
    elif case == "foreign_product":
        node = product(store[3] + "-other", stock="OutOfStock")
    elif case == "wrong_variant":
        store = (*store[:3], store[3] + "?variant=red")
        node = product(store[3].replace("red", "blue"))
        final_url = store[3]
    elif case == "canonical":
        html_prefix = '<link rel="canonical" href="' + store[3] + '-other">'
    elif case == "redirect":
        final_url = store[3] + "-other"
    html = ImagePageHTML(html_prefix + ld(node), final_url)
    result = await scrape(store, html, final_url)
    assert result.price is None
    assert result.available is not False
    assert result.error == "identity_mismatch"


@pytest.mark.parametrize(
    "offer",
    [
        {"price": "100", "priceCurrency": "$"},
        {"price": "$100"},
        {"price": "100", "priceCurrency": "ZZZ"},
    ],
)
def test_eligible_structured_ambiguity_is_terminal(offer):
    result = offer_evidence(offer, strict=True)
    assert result is not None
    assert result["currency_ambiguous"] is True


@pytest.mark.parametrize(
    "excluded",
    [
        {"description": "Monthly installment"},
        {"@type": "AggregateOffer", "lowPrice": "1"},
        {"priceType": "ListPrice"},
    ],
)
def test_excluded_offer_cannot_assert_sold_out(excluded):
    assert offer_evidence({**excluded, "availability": "OutOfStock"}, strict=True) is None


def test_eligible_unpriced_stock_preserves_declared_currency():
    result = offer_evidence({"availability": "OutOfStock", "priceCurrency": "GBP"}, strict=True)
    assert result is not None
    assert (result["available"], result["price"], result["currency"]) == (False, None, "GBP")


def test_legacy_financing_offer_cannot_assert_sold_out():
    assert (
        offer_evidence({"description": "Monthly installment", "availability": "OutOfStock"}) is None
    )


@pytest.mark.parametrize(
    ("store", "html", "currency"),
    [
        (STORES[3], '<div class="O5JUAd">CA$99</div>', "CAD"),
        (
            (*STORES[1][:3], "https://www.apple.com/ca/shop/item"),
            '<div class="rc-prices-fullprice">$99</div>',
            None,
        ),
        (
            STORES[4],
            '<aside><meta itemprop="priceCurrency" content="EUR"></aside>'
            '<div data-test="product-price">£99</div>',
            "GBP",
        ),
        (
            ALL_STORES[-1],
            '<div class="product-price-value">99</div><div class="product-price-currency">$</div>',
            None,
        ),
    ],
)
async def test_dom_currency_belongs_to_selected_price(store, html, currency):
    result = await scrape(store, html)
    if currency is None:
        assert result.price is None
        assert result.error == "currency_ambiguous"
    else:
        assert (result.price, result.currency) == (Decimal("99"), currency)


@pytest.mark.parametrize(
    ("store", "selector"), [(STORES[1], "rc-prices-fullprice"), (STORES[3], "O5JUAd")]
)
async def test_dom_financing_is_not_a_purchase_price(store, selector):
    assert (await scrape(store, f'<div class="{selector}">£10/month</div>')).price is None


async def test_ebay_microdata_currency_can_be_proved_by_its_own_visible_text():
    result = await scrape(
        STORES[-1],
        '<div class="x-price-primary"><span itemprop="price" content="99">US $99</span></div>',
    )
    assert (result.price, result.currency) == (Decimal("99"), "USD")


async def test_walmart_flat_microdata_cannot_borrow_unrelated_currency():
    result = await scrape(
        STORES[5],
        '<aside><meta itemprop="priceCurrency" content="USD"></aside>'
        '<main><span itemprop="price">£99</span></main>',
    )
    assert (result.price, result.currency) == (Decimal("99"), "GBP")


@pytest.mark.parametrize("store", ALL_STORES, ids=lambda s: s[0])
async def test_unpriced_unavailable_keeps_its_verified_currency(store):
    node = product(store[3], stock="OutOfStock")
    del node["offers"]["price"]
    result = await scrape(store, ld(node))
    assert result.available is False
    assert result.price is None
    assert result.currency == "GBP"


async def test_structured_ambiguity_cannot_fall_back_to_a_dom_price():
    node = product(STORES[1][3])
    node["offers"]["priceCurrency"] = "$"
    result = await scrape(STORES[1], ld(node) + '<div class="rc-prices-fullprice">£99</div>')
    assert result.price is None
    assert result.error == "currency_ambiguous"


async def test_aliexpress_distinct_range_is_not_selected_variant():
    data = {
        "data": {
            "priceComponent": {
                "discountedPrice": {
                    "minAmount": {"value": "10", "currency": "USD"},
                    "maxAmount": {"value": "100", "currency": "USD"},
                }
            }
        }
    }
    assert (
        await scrape(
            ALL_STORES[-1], "<script>window.runParams = " + json.dumps(data) + ";</script>"
        )
    ).price is None


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-10", "1000000001"])
async def test_newegg_dom_amount_is_finite_positive_and_bounded(price):
    result = await scrape(
        STORES[7],
        '<div class="product-price"><li class="price-current"><strong>'
        + price
        + "</strong></li></div>",
    )
    assert result.price is None


async def setup_repo(conn: aiosqlite.Connection) -> Repository:
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, Path("src/price_tracker/db/migrations"))
    repo = Repository(conn)
    await repo.ensure_user(1)
    return repo


@pytest.mark.parametrize("explicit_currency", [False, True])
async def test_pending_import_has_known_units_or_is_rejected(explicit_currency):
    async with aiosqlite.connect(":memory:") as conn:
        repo = await setup_repo(conn)
        body = (
            "URL,Valuta\nhttps://www.zalando.co.uk/item,"
            + ("GBP" if explicit_currency else "")
            + "\n"
        )
        update, context, _ = _context(body)
        update.effective_user.first_name = "Fixture"
        update.effective_user.username = "fixture"
        context.bot_data["db"] = repo
        context.bot_data["scraper"].resolve.return_value.scrape.return_value = ProductInfo(
            error="blocked"
        )
        with patch("price_tracker.core.url_utils.validate_public_url", return_value=None):
            await cmd_import(update, context)
        products = await repo.list_products_for_user(user_id=1)
        if not explicit_currency:
            assert products == []
            assert (
                "currency"
                in update.message.reply_text.return_value.edit_text.await_args.args[0].lower()
            )
            return
        registry = ScraperRegistry()
        registry.register(_StubScraper(ProductInfo(price=Decimal("100"), currency="GBP")))
        async with httpx.AsyncClient() as client:
            scheduler = Scheduler(
                SchedulerDeps(
                    repo=repo,
                    registry=registry,
                    client=client,
                    notifier=AsyncMock(),
                    delay_between_products=0,
                )
            )
            await scheduler.run_check_for_user(user_id=1)
        result = await repo.get_product(products[0].id)
        assert result is not None
        assert result.current_price == Decimal("100")
        assert result.currency == "GBP"


@pytest.mark.parametrize(
    "price",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-1"),
        Decimal("0"),
        MAX_AMOUNT + 1,
        "100",
        100,
        True,
        1.5,
    ],
    ids=["nan", "inf", "negative", "zero", "over-limit", "string", "int", "bool", "float"],
)
async def test_scheduler_never_persists_invalid_scraper_money(price):
    async with aiosqlite.connect(":memory:") as conn:
        repo = await setup_repo(conn)
        pid = await repo.add_product(
            user_id=1,
            url="https://example.com/item",
            name="Item",
            domain="example.com",
            initial_price=Decimal("100"),
            currency="GBP",
        )
        registry = ScraperRegistry()
        registry.register(_StubScraper(ProductInfo(price=price, currency="GBP")))
        async with httpx.AsyncClient() as client:
            scheduler = Scheduler(
                SchedulerDeps(
                    repo=repo,
                    registry=registry,
                    client=client,
                    notifier=AsyncMock(),
                    delay_between_products=0,
                )
            )
            await scheduler.run_check_for_user(user_id=1)
        result = await repo.get_product(pid)
        assert result is not None
        assert result.current_price == Decimal("100")
        assert await repo.get_price_history(pid) == []
        assert result.consecutive_errors == 1
