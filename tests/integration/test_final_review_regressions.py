"""Offline desired-behavior checks from the independent final review.

These tests deliberately fail on the frozen candidate. They are review artifacts,
not edits to candidate runtime/tests. No real network or persistent database is used.
"""

import asyncio
import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from price_tracker.bot.handlers._cards import product_view
from price_tracker.bot.messages import reset_locale, set_locale
from price_tracker.bot.ui.cards import CardActions, product_card
from price_tracker.core.alert import _why
from price_tracker.core.exceptions import ListingGone
from price_tracker.core.notices import NoticeCollector
from price_tracker.core.page_image import ImagePageHTML
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scheduler_common import _failure_reason
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.notifier.digest import DigestService
from price_tracker.notifier.preferences import PreferencesManager, update_prefs
from price_tracker.notifier.telegram import TelegramNotifier
from price_tracker.scrapers.amazon import AmazonScraper
from price_tracker.scrapers.generic import GenericScraper
from price_tracker.scrapers.shopify import ShopifyScraper


@pytest.fixture(autouse=True)
def english():
    token = set_locale("en")
    yield
    reset_locale(token)


def jsonld(data: object) -> str:
    return '<script type="application/ld+json">' + json.dumps(data) + "</script>"


@pytest.mark.asyncio
async def test_generic_foreign_offer_cannot_override_matching_product():
    url = "https://store.example.com/p/a?variant=1"
    html = jsonld(
        {
            "@type": "Product",
            "url": url,
            "name": "A variant 1",
            "offers": [
                {
                    "@type": "Offer",
                    "url": url,
                    "price": "90",
                    "priceCurrency": "EUR",
                    "availability": "OutOfStock",
                },
                {
                    "@type": "Offer",
                    "url": "https://store.example.com/p/b?variant=2",
                    "price": "100",
                    "priceCurrency": "EUR",
                    "availability": "InStock",
                },
            ],
        }
    )
    with patch(
        "price_tracker.scrapers.generic._fetch_generic_html",
        AsyncMock(return_value=ImagePageHTML(html, url)),
    ):
        info = await GenericScraper().scrape(url, AsyncMock(spec=httpx.AsyncClient))
    assert info is not None
    assert info.price is None or info.available is False, repr(info)


@pytest.mark.asyncio
async def test_amazon_foreign_offer_cannot_use_matching_product_as_authority():
    url = "https://www.amazon.it/dp/B000000001"
    html = '<h1 id="productTitle">A</h1>' + jsonld(
        {
            "@type": "Product",
            "url": url,
            "offers": {
                "@type": "Offer",
                "url": "https://www.amazon.it/dp/B000000002",
                "price": "80",
                "priceCurrency": "EUR",
                "availability": "InStock",
            },
        }
    )
    with patch(
        "price_tracker.scrapers.amazon._fetch_amazon_page",
        AsyncMock(return_value=ImagePageHTML(html, url)),
    ):
        info = await AmazonScraper().scrape(url, AsyncMock(spec=httpx.AsyncClient))
    assert info.price is None, repr(info)


@pytest.mark.asyncio
async def test_microdata_keeps_currency_and_stock_with_the_selected_offer():
    url = "https://store.example.com/p/a"
    html = (
        '<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">A</span>'
        '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
        '<meta itemprop="priceCurrency" content="USD"></div>'
        '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
        '<meta itemprop="price" content="20"><meta itemprop="priceCurrency" content="EUR">'
        '<link itemprop="availability" href="https://schema.org/OutOfStock"></div></div>'
    )
    with patch(
        "price_tracker.scrapers.generic._fetch_generic_html",
        AsyncMock(return_value=ImagePageHTML(html, url)),
    ):
        info = await GenericScraper().scrape(url, AsyncMock(spec=httpx.AsyncClient))
    assert info.available is False, repr(info)
    assert info.currency != "USD", repr(info)


def test_shopify_selected_offer_stock_is_not_taken_from_excluded_list_price():
    url = "https://store.example.com/products/a"
    offers = [
        {
            "@type": "Offer",
            "url": url + "?variant=1",
            "price": "100",
            "priceCurrency": "EUR",
            "availability": "OutOfStock",
        },
        {
            "@type": "Offer",
            "url": url + "?variant=1",
            "price": "150",
            "priceCurrency": "EUR",
            "priceType": "ListPrice",
            "availability": "InStock",
        },
    ]
    html = jsonld(
        {
            "@type": "ProductGroup",
            "url": url,
            "name": "A",
            "hasVariant": [{"@type": "Product", "sku": "1", "offers": offers}],
        }
    )
    info = ShopifyScraper()._read_product_page(html, url + "?variant=1")
    assert info is not None
    assert info.price is None or info.available is False, repr(info)


@pytest.mark.parametrize("encoding", ["group", "embedded"])
def test_shopify_unpriced_variant_cannot_supply_stock_for_priced_sold_out_variant(encoding):
    url = "https://store.example.com/products/a"
    if encoding == "group":
        html = jsonld(
            {
                "@type": "ProductGroup",
                "url": url,
                "name": "A",
                "hasVariant": [
                    {
                        "@type": "Product",
                        "sku": "1",
                        "offers": {
                            "@type": "Offer",
                            "price": "100",
                            "priceCurrency": "EUR",
                            "availability": "OutOfStock",
                        },
                    },
                    {
                        "@type": "Product",
                        "sku": "2",
                        "offers": {"@type": "Offer", "availability": "InStock"},
                    },
                ],
            }
        )
    else:
        html = (
            '<script id="ProductJson">'
            + json.dumps(
                {
                    "handle": "a",
                    "title": "A",
                    "variants": [
                        {"id": 1, "price": 10000, "price_currency": "EUR", "available": False},
                        {"id": 2, "available": True},
                    ],
                }
            )
            + "</script>"
        )
    info = ShopifyScraper()._read_product_page(html, url)
    assert info is not None
    assert info.price is None or info.available is False, repr(info)


@pytest.mark.asyncio
async def test_shopify_api_rejects_redirect_to_different_store_with_same_handle():
    data = {
        "product": {
            "handle": "a",
            "title": "Foreign store A",
            "variants": [{"id": 1, "price": "80", "price_currency": "EUR", "available": True}],
        }
    }
    response = httpx.Response(
        200,
        json=data,
        request=httpx.Request("GET", "https://another-store.example.net/products/a.json"),
    )
    with patch(
        "price_tracker.scrapers.shopify._fetch_shopify_json", AsyncMock(return_value=response)
    ):
        info = await ShopifyScraper().scrape(
            "https://store.example.com/products/a", AsyncMock(spec=httpx.AsyncClient)
        )
    assert info.price is None, repr(info)


class Scripted(AbstractScraper):
    name = "scripted"
    priority = 100

    def __init__(self, outcomes: list[ProductInfo | BaseException]) -> None:
        self.outcomes = iter(outcomes)

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        value = next(self.outcomes)
        if isinstance(value, BaseException):
            raise value
        return value


@pytest_asyncio.fixture
async def world():
    async with (
        aiosqlite.connect(":memory:") as conn,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as client,
    ):
        conn.row_factory = aiosqlite.Row
        await apply_migrations(conn, Path("src/price_tracker/db/migrations"))
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

        def make(outcomes, notifier=None):
            registry = ScraperRegistry()
            registry.register(Scripted(outcomes))
            return Scheduler(
                SchedulerDeps(
                    repo=repo,
                    registry=registry,
                    client=client,
                    notifier=notifier or AsyncMock(return_value=True),
                    delay_between_products=0,
                    lang="en",
                )
            )

        yield repo, pid, make


@pytest.mark.asyncio
async def test_old_gone_check_cannot_suspend_after_newer_success(world):
    repo, pid, make = world
    await repo.record_failure(pid, reason="listing_gone")
    await repo.record_failure(pid, reason="listing_gone")
    scheduler = make([ListingGone(status=404), ProductInfo(price=Decimal("99"), currency="EUR")])
    ready, release = asyncio.Event(), asyncio.Event()
    original = repo.suspend_product

    async def delayed_suspend(*args, **kwargs):
        ready.set()
        await release.wait()
        return await original(*args, **kwargs)

    repo.suspend_product = delayed_suspend
    old = asyncio.create_task(scheduler.check_one_product_for_user(product_id=pid, user_id=1))
    await asyncio.wait_for(ready.wait(), 3)
    new = asyncio.create_task(scheduler.check_one_product_for_user(product_id=pid, user_id=1))
    try:
        await asyncio.wait_for(asyncio.shield(new), 0.1)
    except TimeoutError:
        pass  # A corrected product lock may hold the second check until old completes.
    finally:
        release.set()
    _, latest = await asyncio.gather(old, new)
    record = await repo.get_product(pid)
    assert latest.reason is not None or record.is_active, repr(record)


@pytest.mark.asyncio
async def test_queued_warning_is_retired_or_marked_historical_after_recovery(world):
    repo, pid, make = world
    await update_prefs(repo, 1, None, digest_mode=True)
    bot = AsyncMock()
    digest = DigestService(repo=repo, bot=bot, lang="en")
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo), digest=digest)
    scheduler = make(
        [ProductInfo(error="unreadable")] * 5 + [ProductInfo(price=Decimal("100"), currency="EUR")],
        notifier,
    )
    for _ in range(6):
        await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
    assert (await repo.get_product(pid)).consecutive_errors == 0
    await digest.flush_user(user_id=1)
    text = "\n".join(call.kwargs["text"] for call in bot.send_message.await_args_list)
    assert "checks failing" not in text, text


def test_processing_error_is_not_presented_as_unreachable_site():
    reason, detail = _failure_reason(ValueError("synthetic parsing problem"))
    assert _why(reason, detail) != "site unreachable"


@pytest.mark.asyncio
async def test_sold_out_card_still_explains_latest_unaccepted_check(world):
    repo, pid, _ = world
    await repo.record_availability(pid, available=False)
    await repo.record_availability(pid, available=False)
    await repo.record_check_outcome(pid, reason="currency_mismatch")
    record = await repo.get_product(pid)
    view = product_view(record, default_interval_minutes=60)
    actions = CardActions(
        check="x", history="x", toggle="x", delete="x", alert_rule="x", interval="x", back="x"
    )
    text = product_card(view, actions, now=datetime.now(UTC)).text
    assert "different currency" in text, text


@pytest.mark.asyncio
async def test_one_warning_envelope_keeps_each_cause_and_independent_cooldown(world):
    repo, pid, make = world
    second = await repo.add_product(
        user_id=1,
        url="https://example.com/p/2",
        name="Other",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    notifier = AsyncMock(return_value=True)
    scheduler = make([], notifier)
    collector = NoticeCollector()
    for product_id, reason in ((pid, "price_none"), (second, "unexpected")):
        for _ in range(5):
            record = await repo.record_failure(product_id, reason=reason)
        collector.add(scheduler._event("warning", record, reason=reason, detail=None))
    await scheduler._flush_notices(collector)
    assert notifier.await_count == 1
    assert notifier.await_args is not None
    text = notifier.await_args.args[1]
    assert "price not readable" in text
    assert "check interrupted" in text
    assert f"#{pid}" in text
    assert f"#{second}" in text
    await scheduler._flush_notices(collector)
    assert notifier.await_count == 1


@pytest.mark.asyncio
async def test_single_warning_uses_singular_and_stock_recovery_has_accepted_amount(world):
    repo, pid, make = world
    notifier = AsyncMock(return_value=True)
    scheduler = make(
        [ProductInfo(error="unreadable")] * 5
        + [ProductInfo(available=False)] * 2
        + [ProductInfo(price=Decimal("100"), currency="EUR")],
        notifier,
    )
    for _ in range(8):
        await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
    texts = [call.args[1] for call in notifier.await_args_list]
    assert "1 products" not in texts[0]
    assert "100" in texts[-1]
    assert "EUR" in texts[-1] or "€" in texts[-1]


@pytest.mark.asyncio
async def test_failed_check_card_explains_cause_only_once(world):
    from tests.unit.test_card_actions_stay import press

    repo, pid, make = world
    query = await press(
        repo, f"check_{pid}", user_id=1, scheduler=make([ProductInfo(error="unreadable")])
    )
    text = (
        query.edit_message_text.await_args.args[0]
        if query.edit_message_text.await_args.args
        else query.edit_message_text.await_args.kwargs["text"]
    )
    assert text.count("The price could not be read.") == 1


@pytest.mark.asyncio
async def test_digest_now_explains_retained_queue_in_italian(world):
    from price_tracker.bot.handlers.settings import digest_now_command

    repo, pid, _ = world
    await update_prefs(repo, 1, pid, mute=True)
    await repo.enqueue_digest(
        user_id=1,
        product_id=pid,
        payload=json.dumps({"product_name": "Widget", "new_price": "90", "currency": "EUR"}),
    )
    digest = DigestService(repo=repo, bot=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=AsyncMock())
    context = SimpleNamespace(bot_data={"digest_service": digest})
    token = set_locale("it_IT")
    try:
        await inspect.unwrap(digest_now_command)(update, context)
    finally:
        reset_locale(token)
    text = update.message.reply_text.await_args.args[0]
    assert "impostazioni" in text
    assert "1" in text
    assert len(await repo.list_pending_digest(user_id=1)) == 1
