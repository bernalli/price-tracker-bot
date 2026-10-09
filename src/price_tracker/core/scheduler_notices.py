"""Operational-notice collection, rendering, locale, and delivery."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from price_tracker.bot.messages import reset_locale, set_locale, user_locale
from price_tracker.core.alert import (
    _why,
    format_quarantine_notification,
    format_warning_notice,
    operational_buttons,
)
from price_tracker.core.notices import NoticeCollector, NoticeGroup, OperationalEvent, group_key_for
from price_tracker.core.scheduler_runtime import format_operational_notice
from price_tracker.core.textlimits import NAME_BUDGET, WHY_BUDGET, truncate_visible

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from price_tracker.core.scheduler_common import SchedulerDeps
    from price_tracker.db.models import ProductRecord

logger = logging.getLogger("price_tracker.core.scheduler")


class NoticeDeliveryMixin:
    """Operational notices and notifier delivery responsibility."""

    deps: SchedulerDeps

    async def _notify_quarantine_entry(
        self, product: ProductRecord, domain: str, *, reason: str
    ) -> None:
        """Push a one-shot alert when ``domain`` first enters quarantine.

        Called only on the CLOSED → LOCKED transition. The notifier runs under a
        broad try/except so a flaky transport never aborts the scheduler tick.
        """
        async with self._recipient_locale(product.user_id):
            message = format_quarantine_notification(
                domain=domain,
                reason=reason,
                locked_until=self.deps.health_mgr.locked_until(domain),
            )
        product_name = truncate_visible(product.name or product.url, NAME_BUDGET)
        await self._notify(
            product.user_id,
            message,
            product_id=None,
            payload={
                "kind": "operational",
                "event": "quarantine",
                "domain": domain,
                "products": [{"id": product.id, "name": product_name, "why": "blocked"}],
                "count": 1,
                "event_id": (
                    f"ops:quarantine:{product.user_id}:{domain}:"
                    f"{self.deps.health_mgr.locked_until(domain) or 'none'}"
                ),
            },
        )

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

        Always records the error and obtains the updated counters in one
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
        if not await self.deps.repo.suspend_product(product.id, reason=reason):
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
            }
            for event in group.events
        ]
        first = group.events[0]
        return {
            "kind": "operational",
            "event": group.event,
            "event_id": (
                f"ops:{group.event}:{group.user_id}:{group.group_key}:"
                f"{sweep_started_at.isoformat()}"
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
        sweep_started_at = datetime.now(UTC)
        for group in collector.groups():
            try:
                async with self._recipient_locale(group.user_id):
                    text = (
                        format_operational_notice(group)
                        if group.event == "suspended"
                        else format_warning_notice(group)
                    )
                    payload = self._operational_payload(group, sweep_started_at)
                delivered = await self._notify(
                    group.user_id, text, product_id=None, payload=payload
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
