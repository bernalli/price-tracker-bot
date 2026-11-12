"""Independent offline witnesses for the final status-notification review."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from price_tracker.bot.callbacks import Action, decode
from price_tracker.core.exceptions import ListingGone
from price_tracker.core.notices import NoticeCollector
from price_tracker.core.scraper_base import ProductInfo
from tests.integration.test_final_review_regressions import world as world


@pytest.mark.asyncio
async def test_warning_waiting_for_flush_is_not_sent_after_newer_success(world):
    repo, pid, make = world
    for _ in range(4):
        await repo.record_failure(pid, reason="price_none")
    notifier = AsyncMock(return_value=True)
    scheduler = make(
        [
            ProductInfo(error="unreadable"),
            ProductInfo(price=Decimal("100"), currency="EUR"),
        ],
        notifier,
    )
    waiting = asyncio.Event()
    succeeded = asyncio.Event()
    original_flush = scheduler._flush_notices
    original_availability = repo.record_availability

    async def observed_flush(collector):
        waiting.set()
        return await original_flush(collector)

    async def observed_availability(*args, **kwargs):
        result = await original_availability(*args, **kwargs)
        if kwargs.get("available") is True:
            succeeded.set()
        return result

    scheduler._flush_notices = observed_flush
    repo.record_availability = observed_availability
    await scheduler._notice_lock.acquire()
    failed = asyncio.create_task(scheduler.check_one_product_for_user(product_id=pid, user_id=1))
    await asyncio.wait_for(waiting.wait(), 2)
    success = asyncio.create_task(scheduler.check_one_product_for_user(product_id=pid, user_id=1))
    await asyncio.wait_for(succeeded.wait(), 2)
    assert (await repo.get_product(pid)).consecutive_errors == 0
    scheduler._notice_lock.release()
    await asyncio.gather(failed, success)
    texts = [call.args[1] for call in notifier.await_args_list]
    assert not any("Checks failing" in text for text in texts), texts


@pytest.mark.asyncio
async def test_pending_status_superseded_before_attempt_is_not_sent(world):
    repo, pid, make = world
    await repo.record_availability(pid, available=False)
    await repo.record_availability(pid, available=False)
    notifier = AsyncMock(return_value=True)
    scheduler = make([ProductInfo(price=Decimal("100"), currency="EUR")], notifier)
    attempt_ready, release_attempt = asyncio.Event(), asyncio.Event()
    original_attempt = repo.mark_status_notice_attempt

    async def delayed_attempt(*args, **kwargs):
        attempt_ready.set()
        await release_attempt.wait()
        return await original_attempt(*args, **kwargs)

    repo.mark_status_notice_attempt = delayed_attempt
    flush = asyncio.create_task(scheduler._flush_notices(NoticeCollector()))
    await asyncio.wait_for(attempt_ready.wait(), 2)
    result = await scheduler._check_product_core(pid, collector=NoticeCollector())
    assert result is not None
    assert result[-1] is None
    assert (await repo.get_product(pid)).is_available is True
    release_attempt.set()
    await flush
    texts = [call.args[1] for call in notifier.await_args_list]
    assert not any("currently unavailable" in text for text in texts), texts


@pytest.mark.asyncio
async def test_periodic_gone_failure_cannot_suspend_after_newer_manual_success(world):
    repo, pid, make = world
    await repo.record_failure(pid, reason="listing_gone")
    await repo.record_failure(pid, reason="listing_gone")
    scheduler = make([ListingGone(status=404), ProductInfo(price=Decimal("100"), currency="EUR")])
    entered, release = asyncio.Event(), asyncio.Event()
    original_suspend = repo.suspend_product

    async def delayed_suspend(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_suspend(*args, **kwargs)

    repo.suspend_product = delayed_suspend
    old = asyncio.create_task(
        scheduler._scrape_one(await repo.get_product(pid), collector=NoticeCollector())
    )
    await asyncio.wait_for(entered.wait(), 2)
    new = asyncio.create_task(scheduler.check_one_product_for_user(product_id=pid, user_id=1))
    try:
        await asyncio.wait_for(asyncio.shield(new), 0.05)
    except TimeoutError:
        pass
    finally:
        release.set()
    _, latest = await asyncio.gather(old, new)
    assert latest.reason is not None or (await repo.get_product(pid)).is_active


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cause", "paused", "foreign", "removed_user"])
async def test_obsolete_warning_members_do_not_send_or_advance_cooldown(world, change):
    repo, pid, make = world
    for _ in range(5):
        record = await repo.record_failure(pid, reason="price_none")
    notifier = AsyncMock(return_value=True)
    scheduler = make([], notifier)
    event = scheduler._event("warning", record, reason="price_none", detail=None)
    if change == "cause":
        await repo.record_failure(pid, reason="unexpected")
    elif change == "paused":
        await repo.pause_product(pid)
    elif change == "foreign":
        await repo.ensure_user(user_id=2)
        event = replace(event, user_id=2)
    else:
        await repo.remove_user(1)
    collector = NoticeCollector()
    collector.add(event)
    await scheduler._flush_notices(collector)
    assert notifier.await_count == 0
    assert await repo.get_config(f"warning_notice:{event.user_id}:example.com:price_none") is None


@pytest.mark.asyncio
async def test_current_warning_refreshes_counter_before_delivery(world):
    repo, pid, make = world
    for _ in range(5):
        record = await repo.record_failure(pid, reason="price_none")
    notifier = AsyncMock(return_value=True)
    scheduler = make([], notifier)
    collector = NoticeCollector()
    collector.add(scheduler._event("warning", record, reason="price_none", detail=None))
    await repo.record_failure(pid, reason="price_none")
    await scheduler._flush_notices(collector)
    assert notifier.await_count == 1
    assert notifier.await_args is not None
    assert "(6/10)" in notifier.await_args.args[1]


@pytest.mark.asyncio
async def test_reactivated_anchor_is_filtered_before_suspension_group_is_rendered(world):
    repo, first, make = world
    second = await repo.add_product(
        user_id=1,
        url="https://example.com/p/2",
        name="Second",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    for pid in (first, second):
        await repo.suspend_product(pid, reason="listing_gone", queue_notice=True)
    notifier = AsyncMock(return_value=True)
    scheduler = make([], notifier)
    original_attempt = repo.mark_status_notice_attempt

    async def reactivate_before_attempt(product_id, *args, **kwargs):
        if product_id == first:
            await repo.reactivate_product(first)
        return await original_attempt(product_id, *args, **kwargs)

    repo.mark_status_notice_attempt = reactivate_before_attempt
    await scheduler._flush_notices(NoticeCollector())
    assert notifier.await_count == 1
    assert notifier.await_args is not None
    message = notifier.await_args.args[1]
    payload = notifier.await_args.kwargs["payload"]
    assert f"#{first}:" not in message
    assert f"#{second}:" in message
    assert payload["product_ids"] == [second]
    assert payload["count"] == 1
    for row in payload["buttons"]:
        for button in row:
            action = decode(button["callback_data"])
            assert isinstance(action, Action)
            assert action.args == (second,)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["acknowledged", "removed_user"])
async def test_attempt_rejects_a_version_with_no_deliverable_notice(world, change):
    repo, pid, _ = world
    await repo.record_availability(pid, available=False)
    await repo.record_availability(pid, available=False)
    record = await repo.get_product(pid)
    if change == "acknowledged":
        await repo.acknowledge_status_notice(pid, record.notice_version)
    else:
        await repo.remove_user(1)
    claimed = await repo.mark_status_notice_attempt(
        pid, record.notice_version, attempted_at=datetime.now(UTC).isoformat()
    )
    assert claimed is False
    assert (await repo.get_product(pid)).notice_attempted_at is None
