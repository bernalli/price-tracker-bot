"""Operational-notice collection, rendering, locale, and delivery."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from price_tracker.bot.messages import reset_locale, set_locale, user_locale
from price_tracker.core.alert import (
    _why,
    format_stock_notice,
    format_store_blocked_notice,
    format_store_reopened_notice,
    format_warning_notice,
    operational_buttons,
)
from price_tracker.core.health import QuarantineState
from price_tracker.core.notices import NoticeCollector, NoticeGroup, OperationalEvent, group_key_for
from price_tracker.core.scheduler_runtime import format_operational_notice
from price_tracker.core.textlimits import NAME_BUDGET, WHY_BUDGET, truncate_visible
from price_tracker.core.url_utils import extract_etld_plus_one

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from price_tracker.core.scheduler_common import SchedulerDeps
    from price_tracker.db.models import ProductRecord

logger = logging.getLogger("price_tracker.core.scheduler")

WARNING_NOTICE_INTERVAL = timedelta(hours=24)
STATUS_RETRY_INTERVAL = timedelta(minutes=15)
STORE_NOTICE_KEY_PREFIX = "store_notice:"
_TOLD_BLOCKED = "blocked"
_TOLD_OPEN = "open"


def store_notice_key(user_id: int, domain: str) -> str:
    """Return the persistent notice key for one recipient and store."""
    return f"{STORE_NOTICE_KEY_PREFIX}{user_id}:{domain}"


def _stored_notice_time(value: object, *, now: datetime) -> datetime | None:
    """The stored notice time, or ``None`` when it is missing, unreadable or ahead of ``now``.

    A time ahead of ``now`` (the clock moved back) is ignored rather than trusted:
    it would otherwise silence the notice until the clock caught up.
    """
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None or when > now:
        return None
    return when


class NoticeDeliveryMixin:
    """Operational notices and notifier delivery responsibility."""

    deps: SchedulerDeps
    _notice_lock: asyncio.Lock

    async def _reconcile_store_notices(self) -> None:
        """Bring every recipient's store notice in line with the store's current state.

        Runs at the end of each periodic sweep, under ``_notice_lock`` for the whole
        round, so two rounds never interleave their reads and writes of the notice
        memory. Recipients are the active users with active products on a store.
        The state of each store is sampled once: LOCKED_T3 and HALF_OPEN_T3 mean
        blocked, CLOSED means open, every other result is left alone. A store that
        changes state during the round is reconciled by the next one.

        Nothing here fails the sweep: an error skips the user, store or recipient
        it concerns, and an error outside them is logged.
        """
        try:
            async with self._notice_lock:
                recipients: dict[str, set[int]] = {}
                for user in await self.deps.repo.list_active_users():
                    try:
                        products = await self.deps.repo.list_products_for_user(
                            user_id=user.user_id, only_active=True
                        )
                    except Exception:  # noqa: BLE001 — one unreadable user must not stop the rest
                        logger.exception(
                            "Could not read the products of user %d for store notices",
                            user.user_id,
                        )
                        continue
                    for product in products:
                        domain = extract_etld_plus_one(product.url)
                        if domain:
                            recipients.setdefault(domain, set()).add(user.user_id)
                for domain in sorted(recipients):
                    try:
                        state = self.deps.health_mgr.state(domain)
                    except Exception:  # noqa: BLE001 — one unreadable store must not stop the rest
                        logger.exception("Could not read the state of %s for store notices", domain)
                        continue
                    if state is QuarantineState.LOCKED_T3 or state is QuarantineState.HALF_OPEN_T3:
                        blocked = True
                    elif state is QuarantineState.CLOSED:
                        blocked = False
                    else:
                        continue
                    for user_id in sorted(recipients[domain]):
                        try:
                            await self._announce_store_to(user_id, domain, blocked=blocked)
                        except Exception:  # noqa: BLE001 — one recipient must not stop the rest
                            logger.exception(
                                "Could not announce the state of %s to user %d", domain, user_id
                            )
        except Exception:  # noqa: BLE001 — notices must never fail the periodic sweep
            logger.exception("Could not reconcile store notices")

    async def _announce_store_to(self, user_id: int, domain: str, *, blocked: bool) -> None:
        """Tell ``user_id`` that ``domain`` blocks the bot, or answers again, if not yet told.

        The caller holds ``_notice_lock``. Only the exact stored value ``"blocked"``
        means the user was last told the store blocks; anything else means open.
        The new value is stored only after the notifier accepted the notice, so a
        refused or failed delivery is retried by the next round.
        """
        repo = self.deps.repo
        key = store_notice_key(user_id, domain)
        stored = await repo.get_config(key)
        if (stored == _TOLD_BLOCKED) == blocked:
            return
        products = await repo.list_products_for_user(user_id=user_id, only_active=True)
        count = sum(1 for product in products if extract_etld_plus_one(product.url) == domain)
        if count == 0:
            return
        async with self._recipient_locale(user_id):
            text = (
                format_store_blocked_notice(domain=domain, products=count)
                if blocked
                else format_store_reopened_notice(domain=domain)
            )
        event = "quarantine" if blocked else "quarantine_end"
        stamp = self.deps.health_mgr.locked_until(domain) if blocked else datetime.now(UTC)
        payload = {
            "kind": "operational",
            "event": event,
            "domain": domain,
            "count": count,
            "event_id": f"ops:{event}:{user_id}:{domain}:{stamp or 'none'}",
        }
        # A user removed while the notice was prepared is not told; nothing runs
        # between this check and the delivery.
        if not await repo.is_user_allowed(user_id):
            return
        if await self._notify(user_id, text, product_id=None, payload=payload):
            await repo.set_config(key, _TOLD_BLOCKED if blocked else _TOLD_OPEN)

    async def _record_failure_and_maybe_disable(
        self,
        product: ProductRecord,
        *,
        scraper_name: str,
        domain: str,
        reason: str,
        detail: str | None = None,
        collector: NoticeCollector,
    ) -> bool:
        """Increment ``consecutive_errors`` and auto-disable on threshold.

        A ``"block"`` is the exception: it is recorded as the product's last
        error and outcome, but it neither counts nor suspends (see below).

        Otherwise records the error and obtains the updated counters in one
        repository operation. When either ``consecutive_errors`` reaches
        ``deps.max_consecutive_errors`` or ``gone_streak`` reaches
        ``deps.listing_gone_confirmations`` it:

        * suspends the product via the compare-and-swap
          :meth:`Repository.suspend_product`
        * adds one grouped operational event to the collector owned by the
          surrounding sweep, which flushes it in ``finally``.

        Returns ``True`` when the product was disabled *by this call* so
        pull-mode callers can flag the disabled status on their
        :class:`CheckResult`.

        ``scraper_name`` and ``domain`` are passed through for structured
        logging only. ``reason`` (plus optional ``detail``) is persisted as the
        product's ``last_error`` so the /errori command can surface it.
        """
        if reason == "block":
            # The store refused the request: the per-store pause owns that
            # condition. Counting it against the product too suspended, one by
            # one, the products of a store that was merely blocked, and left
            # them suspended after the store answered again.
            await self.deps.repo.record_block(product.id, detail=detail)
            updated = None
        else:
            updated = await self.deps.repo.record_failure(product.id, reason=reason, detail=detail)
        # A check that failed produced no evidence about the price, so it breaks
        # any confirmation run in progress: two sightings either side of an
        # outage are not consecutive readings of the same claim.
        if product.pending_read_count or product.pending_read_streak:
            await self.deps.repo.clear_pending_read(product.id)
        if updated is None:
            return False
        max_errors = self.deps.max_consecutive_errors
        half = max_errors // 2
        suspended = (
            updated.consecutive_errors >= max_errors
            or updated.gone_streak >= self.deps.listing_gone_confirmations
        )
        if not suspended:
            if 1 <= half < max_errors and updated.consecutive_errors == half:
                collector.add(self._event("warning", updated, reason=reason, detail=detail))
            return False
        if reason == "listing_gone" and updated.gone_streak < self.deps.listing_gone_confirmations:
            reason = "listing_unconfirmed"
        if not await self.deps.repo.suspend_product(product.id, reason=reason, queue_notice=True):
            # Another check suspended it first, or the user paused it: that
            # check (or the user) owns the outcome, this one stays silent.
            return False
        logger.warning(
            "Product %d auto-disabled after %d consecutive errors "
            "(gone_streak=%d, scraper=%s, domain=%s, reason=%s)",
            product.id,
            updated.consecutive_errors,
            updated.gone_streak,
            scraper_name,
            domain,
            reason,
        )
        updated = await self.deps.repo.get_product(product.id) or updated
        collector.add(self._event("suspended", updated, reason=reason, detail=detail))
        return True

    def _event(
        self,
        event: str,
        record: ProductRecord,
        *,
        reason: str,
        detail: str | None,
    ) -> OperationalEvent:
        """Project one persisted failure into the closed operational event type."""
        return OperationalEvent(
            event=cast("Any", event),
            user_id=record.user_id,
            product_id=record.id,
            product_name=record.name or record.url,
            url=record.url,
            group_key=group_key_for(record.url),
            reason=reason,
            detail=detail,
            last_error=record.last_error,
            error_count=record.consecutive_errors,
            max_errors=self.deps.max_consecutive_errors,
            last_price=record.current_price,
            currency=record.currency,
            last_checked_at=record.last_checked_at,
            notice_version=record.notice_version if event != "warning" else 0,
        )

    def _operational_payload(
        self, group: NoticeGroup, sweep_started_at: datetime
    ) -> dict[str, Any]:
        """Build the JSON-serializable operational-notice contract."""
        products = [
            {
                "id": event.product_id,
                "name": truncate_visible(event.product_name or event.url, NAME_BUDGET),
                "why": truncate_visible(_why(event.reason, event.detail), WHY_BUDGET),
                "reason": event.reason,
                "error_count": event.error_count,
                "max_errors": event.max_errors,
                "notice_version": event.notice_version,
            }
            for event in group.events
        ]
        first = group.events[0]
        durable = all(event.notice_version > 0 for event in group.events)
        identity = ",".join(
            f"{event.product_id}.{event.notice_version}.{event.reason}" for event in group.events
        )
        return {
            "kind": "operational",
            "event": group.event,
            "status_versioned": durable,
            "event_id": (
                f"ops:{group.event}:{group.user_id}:{group.group_key}:{group.primary_reason}:"
                f"{identity if durable else f'{identity}:{sweep_started_at.isoformat()}'}"
            ),
            "user_id": group.user_id,
            "domain": group.group_key,
            "product_ids": [event.product_id for event in group.events],
            "products": products,
            "reason": group.primary_reason,
            "count": len(group.events),
            "error_count": first.error_count,
            "max_errors": first.max_errors,
            "buttons": operational_buttons(group),
        }

    @asynccontextmanager
    async def _recipient_locale(self, user_id: int) -> AsyncIterator[None]:
        """Render in the language of ``user_id``; ``deps.lang`` when none is known."""
        token = set_locale(await user_locale(self.deps.repo, user_id, self.deps.lang))
        try:
            yield
        finally:
            reset_locale(token)

    async def _flush_notices(self, collector: NoticeCollector) -> None:
        """Render and send every group, isolating failures per group."""
        async with self._notice_lock:
            await self._flush_notices_unlocked(collector)

    async def _claim_notice_group(
        self, group: NoticeGroup, *, attempted_at: str
    ) -> NoticeGroup | None:
        """Filter stale members immediately before rendering the delivery attempt.

        The version CAS orders durable attempts against product transitions.
        Warnings carry no version, so refresh their current owner, state and cause.
        A transition after this claim cannot revoke an in-flight external send;
        the acknowledgement still applies only to the claimed version.
        """
        events = []
        for event in group.events:
            if event.event == "warning":
                if not await self.deps.repo.is_user_allowed(event.user_id):
                    continue
                record = await self.deps.repo.get_product_for_user(event.product_id, event.user_id)
                if (
                    record is None
                    or not record.is_active
                    or record.consecutive_errors <= 0
                    or record.last_check_outcome != event.reason
                ):
                    continue
                event = self._event(
                    "warning", record, reason=event.reason or "unknown", detail=record.last_error
                )
            elif event.notice_version and not await self.deps.repo.mark_status_notice_attempt(
                event.product_id, event.notice_version, attempted_at=attempted_at
            ):
                continue
            events.append(event)
        if not events:
            return None
        return NoticeGroup(group.event, group.user_id, group.group_key, tuple(events))

    async def _flush_notices_unlocked(self, collector: NoticeCollector) -> None:
        """Merge ephemeral warnings with durable, due status transitions.

        The product UPDATE that changes status also queues its notice. Therefore
        a crash before this flush, or a rejected Telegram send, cannot silently
        lose a suspension. Newer stock transitions supersede obsolete notices.
        """
        sweep_started_at = datetime.now(UTC)
        pending = NoticeCollector()
        for group in collector.groups():
            for event in group.events:
                if event.event == "warning":
                    key = (
                        f"warning_notice:{event.user_id}:{event.group_key}:"
                        f"{event.reason or 'unknown'}"
                    )
                    last = _stored_notice_time(
                        await self.deps.repo.get_config(key), now=sweep_started_at
                    )
                    if last is not None and sweep_started_at - last < WARNING_NOTICE_INTERVAL:
                        continue
                if event.event == "warning" or event.notice_version == 0:
                    pending.add(event)
        for record in await self.deps.repo.list_pending_status_notices():
            attempted = _stored_notice_time(record.notice_attempted_at, now=sweep_started_at)
            if attempted is not None and sweep_started_at - attempted < STATUS_RETRY_INTERVAL:
                continue
            event_kind = record.pending_notice_kind
            if event_kind not in {"suspended", "unavailable", "restocked"}:
                continue
            pending.add(
                self._event(
                    event_kind,
                    record,
                    reason=(record.suspension_reason or "unknown")
                    if event_kind == "suspended"
                    else event_kind,
                    detail=record.last_error,
                )
            )
        for group in pending.groups():
            try:
                async with self._recipient_locale(group.user_id):
                    claimed = await self._claim_notice_group(
                        group, attempted_at=sweep_started_at.isoformat()
                    )
                    if claimed is None:
                        continue
                    group = claimed
                    if group.event == "suspended":
                        text = format_operational_notice(group)
                    elif group.event == "warning":
                        text = format_warning_notice(group)
                    else:
                        text = format_stock_notice(group.events[0])
                    payload = self._operational_payload(group, sweep_started_at)
                product_id = None
                if group.event in {"unavailable", "restocked"}:
                    item = group.events[0]
                    product_id = item.product_id
                    payload.update(
                        kind="availability",
                        product_name=item.product_name,
                        last_price=str(item.last_price) if item.last_price is not None else None,
                        currency=item.currency,
                    )
                delivered = await self._notify(
                    group.user_id, text, product_id=product_id, payload=payload
                )
                if delivered:
                    if group.event == "warning":
                        for reason in {event.reason or "unknown" for event in group.events}:
                            warning_key = (
                                f"warning_notice:{group.user_id}:{group.group_key}:{reason}"
                            )
                            await self.deps.repo.set_config(
                                warning_key, sweep_started_at.isoformat()
                            )
                    for event in group.events:
                        if event.notice_version:
                            await self.deps.repo.acknowledge_status_notice(
                                event.product_id, event.notice_version
                            )
                if not delivered:
                    logger.warning(
                        "Operational notice was not delivered (user_id=%d, group_key=%s, "
                        "product_ids=%s)",
                        group.user_id,
                        group.group_key,
                        [event.product_id for event in group.events],
                    )
            except Exception:  # noqa: BLE001 — one group must not block the remaining groups
                logger.exception(
                    "Failed to render or deliver operational notice (user_id=%d, group_key=%s)",
                    group.user_id,
                    group.group_key,
                )

    async def _flush_guaranteed(self, collector: NoticeCollector) -> None:
        """Flush once; cancellation cannot cancel the independently shielded flush task."""
        task = asyncio.create_task(self._flush_notices(collector))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        await task
        if cancelled:
            raise asyncio.CancelledError

    async def _notify(
        self,
        user_id: int,
        message: str,
        *,
        product_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Push one message, reporting whether it actually reached the user.

        A notifier that raises, or that answers ``False``, did not deliver —
        callers must not record bookkeeping (cooldowns, alert timestamps) off a
        message nobody received. Exceptions never escape: one undeliverable
        message must not abort the rest of the tick.
        """
        try:
            delivered = await self.deps.notifier(
                user_id, message, product_id=product_id, payload=payload
            )
        except Exception:  # noqa: BLE001 — notifier failure must not kill the tick
            logger.exception("Notifier failed to deliver a message to user %d", user_id)
            return False
        return delivered is not False
