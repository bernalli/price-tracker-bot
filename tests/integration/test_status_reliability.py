"""Regression coverage for truthful, durable product-status notifications."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from freezegun import freeze_time

from price_tracker.core.exceptions import ListingGone
from price_tracker.core.health import HealthManager
from price_tracker.core.notices import NoticeCollector, OperationalEvent
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.notifier.digest import DigestService
from price_tracker.notifier.preferences import PreferencesManager, update_prefs
from price_tracker.notifier.telegram import TelegramNotifier


class ScriptedScraper(AbstractScraper):
    name = "scripted"
    priority = 100

    def __init__(self, outcomes: list[ProductInfo | BaseException]) -> None:
        self.outcomes = iter(outcomes)

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest_asyncio.fixture
async def world():
    async with aiosqlite.connect(":memory:") as conn:
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
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        ) as client:

            def make(outcomes, notifier=None, **kwargs):
                registry = ScraperRegistry()
                registry.register(ScriptedScraper(outcomes))
                return Scheduler(
                    SchedulerDeps(
                        repo=repo,
                        registry=registry,
                        client=client,
                        notifier=notifier or AsyncMock(return_value=True),
                        delay_between_products=0,
                        lang="en",
                        **kwargs,
                    )
                )

            yield repo, pid, make


GOOD = ProductInfo(price=Decimal("100"), currency="EUR")
BAD = ProductInfo(error="unreadable")
SOLD = ProductInfo(available=False)


def test_mixed_suspension_causes_never_share_one_removal_claim():
    collector = NoticeCollector()
    for pid, reason in enumerate(("listing_gone", "listing_gone", "parse_error"), 1):
        collector.add(
            OperationalEvent(
                event="suspended",
                user_id=1,
                product_id=pid,
                product_name=f"Product {pid}",
                url=f"https://example.com/{pid}",
                group_key="example.com",
                reason=reason,
                detail=None,
                last_error=reason,
                error_count=10,
                max_errors=10,
                last_price=Decimal("100"),
                currency="EUR",
                last_checked_at=None,
            )
        )
    assert [(g.primary_reason, len(g.events)) for g in collector.groups()] == [
        ("listing_gone", 2),
        ("parse_error", 1),
    ]


@pytest.mark.asyncio
async def test_generic_error_limit_does_not_confirm_one_missing_page(world):
    repo, pid, make = world
    notify = AsyncMock(return_value=True)
    scheduler = make([BAD] * 9 + [ListingGone(status=404)], notify)
    for _ in range(10):
        await scheduler.run_check_for_user(user_id=1)
    product = await repo.get_product(pid)
    assert product.gone_streak == 1
    assert product.is_active is False
    assert product.suspension_reason == "listing_unconfirmed"
    assert notify.await_args is not None
    text = notify.await_args.args[1]
    assert "removed" not in text.lower()
    assert "unconfirmed" in text.lower()


@pytest.mark.asyncio
async def test_warning_cooldown_survives_recovery_and_scheduler_restart(world):
    repo, pid, make = world
    notify = AsyncMock(return_value=True)
    for sequence in ([BAD] * 5 + [GOOD], [BAD] * 5):
        scheduler = make(sequence, notify)
        for _ in sequence:
            await scheduler.run_check_for_user(user_id=1)
    assert len(notify.await_args_list) == 1
    # A final state must still get through even inside the warning cooldown.
    scheduler = make([BAD] * 5, notify)
    for _ in range(5):
        await scheduler.run_check_for_user(user_id=1)
    assert [c.kwargs["payload"]["event"] for c in notify.await_args_list] == [
        "warning",
        "suspended",
    ]


@pytest.mark.asyncio
async def test_sold_out_confirmation_and_transition_notice_survive_restart(world):
    repo, pid, make = world
    notify = AsyncMock(return_value=True)
    for reading in (SOLD, SOLD, SOLD, GOOD, GOOD):
        await make([reading], notify).run_check_for_user(user_id=1)
    product = await repo.get_product(pid)
    assert product.is_available is True
    assert product.consecutive_errors == 0
    events = [c.kwargs["payload"]["event"] for c in notify.await_args_list]
    assert events == ["unavailable", "restocked"]


@pytest.mark.asyncio
async def test_rejected_suspension_notice_retries_after_restart_without_scraping(world):
    repo, pid, make = world
    notify = AsyncMock(return_value=False)
    with freeze_time("2026-11-12 10:00:00"):
        scheduler = make([ListingGone(status=404)] * 3, notify)
        for _ in range(3):
            await scheduler.run_check_for_user(user_id=1)
        assert notify.await_args is not None
        first_id = notify.await_args.kwargs["payload"]["event_id"]
        await make([], notify).run_check_for_user(user_id=1)
        assert notify.await_count == 1
    with freeze_time("2026-11-12 10:16:00"):
        notify.return_value = True
        await make([], notify).run_check_for_user(user_id=1)
        assert notify.await_count == 2
        assert notify.await_args is not None
        assert notify.await_args.kwargs["payload"]["event_id"] == first_id
        await make([], notify).run_check_for_user(user_id=1)
        assert notify.await_count == 2
    assert (await repo.get_product(pid)).is_active is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("info", "reason"),
    [
        (BAD, "price_none"),
        (ProductInfo(error="currency_ambiguous"), "currency_unknown"),
        (ProductInfo(price=Decimal("100"), currency="USD"), "currency_mismatch"),
        (ProductInfo(price=Decimal("100"), currency="EUR", condition="used"), "condition_mismatch"),
    ],
)
async def test_nonaccepted_read_has_explicit_pull_reason(world, info, reason):
    repo, pid, make = world
    await repo.set_product_preferences(pid, condition="new")
    result = await make([info]).check_one_product_for_user(product_id=pid, user_id=1)
    assert result.reason == reason
    assert (await repo.get_product(pid)).current_price == Decimal("100")
    assert await repo.get_price_history(pid) == []


@pytest.mark.asyncio
async def test_every_requested_id_gets_an_explicit_skip_outcome(world):
    repo, pid, make = world
    await repo.pause_product(pid)
    scheduler = make([])
    results = await scheduler.check_products_for_user(product_ids=[pid, 999], user_id=1)
    assert [(r.product_id, r.reason) for r in results] == [(pid, "inactive"), (999, "not_found")]


@pytest.mark.asyncio
async def test_quarantine_has_an_explicit_pull_outcome(world):
    repo, pid, make = world
    health = HealthManager(repo)
    for _ in range(3):
        await health.record_block("example.com", reason="blocked")
    result = await make([], health_mgr=health).check_one_product_for_user(product_id=pid, user_id=1)
    assert result.reason == "quarantined"


@pytest.mark.asyncio
async def test_no_scraper_and_held_reads_have_explicit_pull_reasons(world):
    repo, pid, make = world
    scheduler = make([])
    scheduler.deps.registry = ScraperRegistry()
    assert (
        await scheduler.check_one_product_for_user(product_id=pid, user_id=1)
    ).reason == "no_scraper"
    # Stable history makes a large but plausible change require confirmation.
    for _ in range(5):
        await repo.add_price_history(pid, Decimal("100"))
    result = await make(
        [ProductInfo(price=Decimal("40"), currency="EUR")]
    ).check_one_product_for_user(product_id=pid, user_id=1)
    assert result.reason == "awaiting_confirmation"
    result = await make(
        [ProductInfo(price=Decimal("0.01"), currency="EUR")]
    ).check_one_product_for_user(product_id=pid, user_id=1)
    assert result.reason == "implausible_read"


@pytest.mark.asyncio
async def test_simultaneous_digest_flushes_send_each_entry_once(world):
    repo, pid, _ = world
    await repo.enqueue_digest(
        user_id=1, product_id=pid, payload='{"product_name":"Widget","new_price":"80"}'
    )
    started, release = asyncio.Event(), asyncio.Event()
    messages = []

    async def send(**kwargs):
        messages.append(kwargs)
        started.set()
        await release.wait()

    bot = AsyncMock()
    bot.send_message.side_effect = send
    service = DigestService(repo=repo, bot=bot)
    first = asyncio.create_task(service.flush_user(user_id=1))
    await started.wait()
    second = asyncio.create_task(service.flush_user(user_id=1))
    # Let the overlapping caller reach the flush while the first owns delivery.
    for _ in range(30):
        await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert tuple(results) == (1, 0)
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_failed_digest_recipient_does_not_block_other_users(world):
    repo, pid, _ = world
    await repo.ensure_user(user_id=2)
    for user in (1, 2):
        await repo.enqueue_digest(user_id=user, product_id=None, payload='{"kind":"operational"}')
    messages = []

    async def send(**kwargs):
        if kwargs["chat_id"] == 1:
            raise RuntimeError("chat unavailable")
        messages.append(kwargs)

    bot = AsyncMock()
    bot.send_message.side_effect = send
    with freeze_time(datetime.now(UTC) + timedelta(hours=2)):
        assert await DigestService(repo=repo, bot=bot).flush_due(interval_minutes=60) == 1
    assert [message["chat_id"] for message in messages] == [2]
    assert len(await repo.list_pending_digest(user_id=1)) == 1


@pytest.mark.asyncio
async def test_status_is_durable_before_the_sweep_flush_and_due_without_active_products(world):
    repo, pid, make = world
    notify = AsyncMock(return_value=True)
    scheduler = make([ListingGone(status=410)] * 3, notify)
    for _ in range(3):
        product = await repo.get_product(pid)
        await scheduler._scrape_one(product, collector=NoticeCollector())
    product = await repo.get_product(pid)
    assert product.pending_notice_kind == "suspended"
    assert product.is_active is False
    assert notify.await_count == 0
    # A new process sees no active checks but must still drain the pending status.
    await make([], notify).run_check_due(global_interval_minutes=60)
    assert notify.await_count == 1
    assert (await repo.get_product(pid)).pending_notice_kind is None


@pytest.mark.asyncio
async def test_old_delivery_cannot_acknowledge_a_newer_stock_transition(world):
    repo, pid, _ = world
    await repo.record_availability(pid, available=False)
    await repo.record_availability(pid, available=False)
    old = await repo.get_product(pid)
    await repo.record_availability(pid, available=True)
    await repo.acknowledge_status_notice(pid, old.notice_version)
    current = await repo.get_product(pid)
    assert current.pending_notice_kind == "restocked"
    assert current.notice_version > old.notice_version


@pytest.mark.asyncio
async def test_unavailable_notice_obeys_mute_and_digest_preferences(world):
    repo, pid, make = world
    bot = AsyncMock()
    digest = DigestService(repo=repo, bot=bot)
    notifier = TelegramNotifier(bot=bot, prefs=PreferencesManager(repo), digest=digest)
    await update_prefs(repo, 1, pid, mute=True)
    scheduler = make([SOLD, SOLD], notifier)
    for _ in range(2):
        await scheduler.run_check_for_user(user_id=1)
    assert bot.send_message.await_count == 0
    assert await repo.list_pending_digest(user_id=1) == []
    await update_prefs(repo, 1, pid, mute=False, digest_mode=True)
    with freeze_time(datetime.now(UTC) + timedelta(minutes=16)):
        await make([], notifier).run_check_due(global_interval_minutes=60)
    pending = await repo.list_pending_digest(user_id=1)
    assert len(pending) == 1
    assert '"event": "unavailable"' in pending[0].alert_payload_json
    assert (await repo.get_product(pid)).pending_notice_kind is None
    await digest.flush_user(user_id=1)
    assert "unavailable" in bot.send_message.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_warning_causes_share_a_message_but_keep_distinct_delivery_identities(world):
    repo, pid, make = world
    second = await repo.add_product(
        user_id=1,
        url="https://example.com/second",
        name="Second",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    bot = AsyncMock()
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo))
    scheduler = make([], notifier)
    collector = NoticeCollector()
    for product_id, reason in ((pid, "price_none"), (second, "unexpected")):
        for _ in range(5):
            product = await repo.record_failure(product_id, reason=reason)
        collector.add(scheduler._event("warning", product, reason=reason, detail=None))
    await scheduler._flush_notices(collector)
    texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert len(texts) == 1
    assert f"#{pid}: price not readable" in texts[0]
    assert f"#{second}: check interrupted" in texts[0]
    assert await repo.get_config("warning_notice:1:example.com:price_none") is not None
    assert await repo.get_config("warning_notice:1:example.com:unexpected") is not None


@pytest.mark.asyncio
async def test_digest_suppresses_superseded_stock_status_after_restart(world):
    repo, pid, make = world
    bot = AsyncMock()
    digest = DigestService(repo=repo, bot=bot, lang="en")
    await update_prefs(repo, 1, pid, digest_mode=True)
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo), digest=digest)
    for reading in (SOLD, SOLD, GOOD):
        await make([reading], notifier).run_check_for_user(user_id=1)
    await DigestService(repo=repo, bot=bot, lang="en").flush_user(user_id=1)
    text = bot.send_message.await_args.kwargs["text"]
    assert "currently unavailable" not in text
    assert "available again" in text
    assert f"#{pid}" in text
    assert await repo.list_pending_digest(user_id=1) == []


@pytest.mark.asyncio
async def test_digest_removes_reactivated_members_of_suspension_group(world):
    repo, pid, make = world
    second = await repo.add_product(
        user_id=1,
        url="https://example.com/second",
        name="Second",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    bot = AsyncMock()
    digest = DigestService(repo=repo, bot=bot, lang="en")
    await update_prefs(repo, 1, None, digest_mode=True)
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo), digest=digest)
    for product_id in (pid, second):
        await repo.suspend_product(product_id, reason="parse_error", queue_notice=True)
    await make([], notifier)._flush_notices(NoticeCollector())
    await repo.reactivate_product(pid)
    await digest.flush_user(user_id=1)
    text = bot.send_message.await_args.kwargs["text"]
    assert f"#{pid}:" not in text
    assert f"#{second}:" in text
    assert "Widget" not in text
    assert "Second" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_digest_honors_mute_changed_after_enqueue(world, manual):
    repo, pid, make = world
    bot = AsyncMock()
    digest = DigestService(repo=repo, bot=bot, lang="en")
    await update_prefs(repo, 1, pid, digest_mode=True)
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo), digest=digest)
    for reading in (SOLD, SOLD):
        await make([reading], notifier).run_check_for_user(user_id=1)
    await update_prefs(repo, 1, pid, mute=True)
    with freeze_time(datetime.now(UTC) + timedelta(hours=2)):
        if manual:
            await digest.flush_user(user_id=1)
        else:
            await digest.flush_due(interval_minutes=60)
    assert bot.send_message.await_count == 0
    assert len(await repo.list_pending_digest(user_id=1)) == 1
    await update_prefs(repo, 1, pid, mute=False)
    await digest.flush_user(user_id=1)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_digest_honors_product_quiet_hours_at_delivery(world):
    repo, pid, make = world
    bot = AsyncMock()
    digest = DigestService(repo=repo, bot=bot, lang="en")
    await update_prefs(
        repo,
        1,
        pid,
        digest_mode=True,
        timezone="UTC",
        quiet_hours_start="09:00",
        quiet_hours_end="13:00",
    )
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo), digest=digest)
    with freeze_time("2026-11-12 10:00:00"):
        for reading in (SOLD, SOLD):
            await make([reading], notifier).run_check_for_user(user_id=1)
    # SQLite CURRENT_TIMESTAMP uses the host clock, not freezegun's clock.
    # Anchor the queued row to the simulated enqueue time before testing age.
    await repo._conn.execute(
        "UPDATE digest_queue SET enqueued_at = ? WHERE user_id = ?",
        ("2026-11-12 10:00:00", 1),
    )
    await repo._conn.commit()
    with freeze_time("2026-11-12 12:00:00"):
        await digest.flush_due(interval_minutes=60)
        await digest.flush_user(user_id=1)
    assert bot.send_message.await_count == 0
    with freeze_time("2026-11-12 14:00:00"):
        await digest.flush_due(interval_minutes=60)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("info", "reason"),
    [
        (ProductInfo(price=Decimal("100"), currency="USD"), "currency_mismatch"),
        (ProductInfo(price=Decimal("40"), currency="EUR"), "awaiting_confirmation"),
    ],
)
async def test_card_retains_unaccepted_check_reason_until_a_price_is_accepted(world, info, reason):
    from price_tracker.bot.handlers._cards import product_view
    from price_tracker.bot.ui.cards import _now_line

    repo, pid, make = world
    for _ in range(5):
        await repo.add_price_history(pid, Decimal("100"))
    before = await repo.get_product(pid)
    result = await make([info]).check_one_product_for_user(product_id=pid, user_id=1)
    assert result.reason == reason
    # Build a fresh repository/view, as a later card request after restart would.
    fresh = Repository(repo._conn)
    row = await fresh.get_product(pid)
    assert row is not None
    assert before is not None
    view = product_view(row, default_interval_minutes=60)
    assert "Last recorded price" in _now_line(view, loc="en")
    assert row.last_check_outcome == reason
    assert row.last_checked_at == before.last_checked_at
    await make([GOOD]).check_one_product_for_user(product_id=pid, user_id=1)
    row = await fresh.get_product(pid)
    assert row is not None
    assert row.last_check_outcome is None
    assert "Now:" in _now_line(product_view(row, default_interval_minutes=60), loc="en")


@pytest.mark.asyncio
async def test_held_price_has_pending_mark_in_list_and_record_rows(world):
    from price_tracker.bot.handlers._cards import product_view
    from price_tracker.bot.ui.cards import list_mark
    from price_tracker.bot.ui.product_rows import record_row

    repo, pid, make = world
    for _ in range(5):
        await repo.add_price_history(pid, Decimal("100"))
    await make([ProductInfo(price=Decimal("40"), currency="EUR")]).check_one_product_for_user(
        product_id=pid, user_id=1
    )
    row = await repo.get_product(pid)
    assert list_mark(product_view(row, default_interval_minutes=60)) == "⏳"
    assert "⏳" in record_row(row)
    assert row.consecutive_errors == 0


@pytest.mark.asyncio
async def test_global_sweep_skip_is_visible_on_later_card(world):
    repo, pid, make = world
    health = HealthManager(repo)
    for _ in range(3):
        await health.record_block("example.com", reason="blocked")
    await make([], health_mgr=health).run_check_for_user(user_id=1)
    assert (await repo.get_product(pid)).last_check_outcome == "quarantined"


@pytest.mark.asyncio
async def test_status_digest_enqueue_is_idempotent_after_acknowledgement_failure(world):
    repo, pid, make = world
    bot = AsyncMock()
    digest = DigestService(repo=repo, bot=bot, lang="en")
    await update_prefs(repo, 1, pid, digest_mode=True)
    original_acknowledge = repo.acknowledge_status_notice
    repo.acknowledge_status_notice = AsyncMock(side_effect=RuntimeError("crash after enqueue"))
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo), digest=digest)
    for reading in (SOLD, SOLD):
        await make([reading], notifier).run_check_for_user(user_id=1)
    repo.acknowledge_status_notice = original_acknowledge
    # A fresh scheduler/notifier process retries the still-pending status.
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo), digest=digest)
    with freeze_time(datetime.now(UTC) + timedelta(minutes=16)):
        await make([], notifier)._flush_notices(NoticeCollector())
    assert len(await repo.list_pending_digest(user_id=1)) == 1
    await digest.flush_user(user_id=1)
    assert bot.send_message.await_count == 1
    assert (await repo.get_product(pid)).pending_notice_kind is None


@pytest.mark.asyncio
async def test_stock_status_with_foreign_product_id_is_not_delivered(world):
    repo, pid, _ = world
    await repo.ensure_user(user_id=2)
    await repo.record_availability(pid, available=False)
    await repo.record_availability(pid, available=False)
    row = await repo.get_product(pid)
    digest = DigestService(repo=repo, bot=AsyncMock(), lang="en")
    await digest.enqueue(
        user_id=2,
        product_id=None,
        payload={
            "kind": "availability",
            "event": "unavailable",
            "status_versioned": True,
            "products": [{"id": pid, "notice_version": row.notice_version, "name": "Private"}],
        },
    )
    assert await digest.flush_user(user_id=2) == 0
    assert await repo.list_pending_digest(user_id=2) == []


@pytest.mark.asyncio
async def test_later_restock_discards_muted_obsolete_unavailable_without_sending(world):
    repo, pid, make = world
    bot = AsyncMock()
    digest = DigestService(repo=repo, bot=bot, lang="en")
    await update_prefs(repo, 1, pid, digest_mode=True)
    notifier = TelegramNotifier(bot, prefs=PreferencesManager(repo), digest=digest)
    for reading in (SOLD, SOLD):
        await make([reading], notifier).run_check_for_user(user_id=1)
    await update_prefs(repo, 1, pid, mute=True)
    await make([GOOD], notifier).run_check_for_user(user_id=1)
    assert await digest.flush_user(user_id=1) == 0
    assert await repo.list_pending_digest(user_id=1) == []
    assert bot.send_message.await_count == 0
    assert (await repo.get_product(pid)).pending_notice_kind == "restocked"
