"""Single-product checks, availability, read confirmation, and pull mode."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx

from price_tracker.core.alert import (
    PriceAlert,
    ThresholdType,
    crosses_threshold,
    format_alert,
    format_back_in_stock,
)
from price_tracker.core.exceptions import BlockEvent, ListingGone, ParseError
from price_tracker.core.health import QuarantineState
from price_tracker.core.notices import NoticeCollector
from price_tracker.core.outlier import MAX_HELD_READS, ReadVerdict, classify_read, reads_agree
from price_tracker.core.scheduler_common import (
    CheckResult,
    SchedulerDeps,
    _alert_payload,
    _failure_reason,
    _parse_db_timestamp,
)
from price_tracker.core.scheduler_runtime import handle_success_in_pipeline
from price_tracker.core.scraper_base import handle_block_in_pipeline
from price_tracker.core.url_utils import extract_etld_plus_one

if TYPE_CHECKING:
    from decimal import Decimal

    from price_tracker.db.models import ProductRecord

logger = logging.getLogger("price_tracker.core.scheduler")


class ProductChecksMixin:
    """Product-check pipeline and interactive pull-mode responsibility."""

    deps: SchedulerDeps
    _attempted_at: dict[int, datetime]
    _sold_out_streaks: dict[int, int]
    _product_locks: dict[int, asyncio.Lock]

    if TYPE_CHECKING:

        async def _notify_quarantine_entry(
            self, product: ProductRecord, domain: str, *, reason: str
        ) -> None: ...

        async def _record_failure_and_maybe_disable(
            self,
            product: ProductRecord,
            *,
            scraper_name: str,
            domain: str,
            reason: str,
            detail: str | None = None,
            collector: NoticeCollector,
        ) -> bool: ...

        async def _notify(
            self,
            user_id: int,
            message: str,
            *,
            product_id: int | None = None,
            payload: dict[str, Any] | None = None,
        ) -> bool: ...

        async def _flush_guaranteed(self, collector: NoticeCollector) -> None: ...

        def _recipient_locale(self, user_id: int) -> Any: ...

    async def _check_product_core(
        self,
        product_id: int,
        *,
        scraper_name: str = "unknown",
        domain: str = "unknown",
        collector: NoticeCollector,
    ) -> tuple[int, PriceAlert | None, bool, str | None] | None:
        """Serialize every check of one product within this scheduler."""
        lock = self._product_locks.setdefault(product_id, asyncio.Lock())
        async with lock:
            return await self._check_product_core_unlocked(
                product_id,
                scraper_name=scraper_name,
                domain=domain,
                collector=collector,
            )

    async def _check_product_core_unlocked(
        self,
        product_id: int,
        *,
        scraper_name: str = "unknown",
        domain: str = "unknown",
        collector: NoticeCollector,
    ) -> tuple[int, PriceAlert | None, bool, str | None] | None:
        """Scrape one product, persist, and return ``(user_id, alert, disabled, reason)``.

        * ``alert`` is set only when the new price actually crossed the threshold.
        * ``disabled`` is ``True`` when this call brought ``consecutive_errors``
          to ``max_consecutive_errors`` and the product was auto-paused.
        * ``reason`` is ``"out_of_stock"`` when the scraper positively recognised a
          sold-out listing, and ``None`` otherwise.
        * Returns ``None`` when the product is missing or already inactive.

        Side-effects: writes price/history/errors to the repository and emits
        metrics. Auto-disable and warning events are added to ``collector``;
        price-drop alerts are returned to the caller, which decides whether to
        push (periodic job) or return them (interactive handler).
        """
        p = await self.deps.repo.get_product(product_id)
        if p is None or not p.is_active:
            return None
        self._attempted_at[p.id] = datetime.now(UTC)

        scraper = self.deps.registry.resolve(p.url)
        if scraper is None:
            logger.warning("No scraper for %s", p.url)
            metrics = self.deps.metrics
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            disabled = await self._record_failure_and_maybe_disable(
                p,
                scraper_name=scraper_name,
                domain=domain,
                reason="no_scraper",
                collector=collector,
            )
            return (p.user_id, None, disabled, None)

        metrics = self.deps.metrics
        if metrics is not None:
            with metrics.scraper_duration_seconds.labels(
                scraper=scraper_name, domain=domain
            ).time():
                info = await scraper.scrape(p.url, self.deps.client)
        else:
            info = await scraper.scrape(p.url, self.deps.client)

        if info.available is False:
            # The scraper positively recognised a sold-out listing: whatever price
            # is left on the page (none, or a placeholder) is not a reading, and
            # "no price" here is the state being observed, not a parsing failure.
            # Treating it as one would suspend the product after
            # max_consecutive_errors and the user would never hear about the
            # restock. Only an explicit False counts: a missing price with the
            # default availability is a layout change and stays a failure below.
            sold_out_streak = self._sold_out_streaks.get(p.id, 0) + 1
            self._sold_out_streaks[p.id] = sold_out_streak
            if sold_out_streak >= 2 and p.is_available is not False:
                await self.deps.repo.set_availability(p.id, available=False)
            if p.pending_read_count or p.pending_read_streak:
                await self.deps.repo.clear_pending_read(p.id)
            await self.deps.repo.reset_errors(p.id)
            await self.deps.repo.mark_checked(p.id)
            if domain != "unknown":
                await handle_success_in_pipeline(health_mgr=self.deps.health_mgr, domain=domain)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="success"
                ).inc()
            return (p.user_id, None, False, "out_of_stock")

        if info.available and info.price is not None:
            self._sold_out_streaks[p.id] = 0

        if info.price is None:
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            disabled = await self._record_failure_and_maybe_disable(
                p,
                scraper_name=scraper_name,
                domain=domain,
                reason="price_none",
                detail=info.error,
                collector=collector,
            )
            return (p.user_id, None, disabled, None)

        if info.currency is not None and p.currency is not None and info.currency != p.currency:
            logger.warning(
                "Product %d: currency mismatch (scraped=%s, stored=%s) — read skipped, "
                "no persist/alert",
                p.id,
                info.currency,
                p.currency,
            )
            # A currency mismatch is still a successful scrape (HTTP ok, price
            # parsed) — record it so a HALF_OPEN probe can close the domain;
            # only the persist/alert is skipped (#20).
            if domain != "unknown":
                await handle_success_in_pipeline(health_mgr=self.deps.health_mgr, domain=domain)
            if p.pending_read_count or p.pending_read_streak:
                await self.deps.repo.clear_pending_read(p.id)
            await self.deps.repo.reset_errors(p.id)
            return (p.user_id, None, False, None)

        if not self._condition_matches(p, info.condition):
            logger.info(
                "Product %d: buy-box offer is %r but the user tracks %r — read skipped, "
                "no persist/alert",
                p.id,
                info.condition,
                p.preferred_condition,
            )
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="condition_mismatch"
                ).inc()
            # Still a successful fetch: let a HALF_OPEN domain close on it (#20).
            if domain != "unknown":
                await handle_success_in_pipeline(health_mgr=self.deps.health_mgr, domain=domain)
            # Counted as a failure on purpose. Skipping the read is right — a
            # different offer is a different price — but doing it silently would
            # leave the product frozen on a stale price while still looking
            # healthy. Recording it surfaces the product in /errori and, if no
            # matching offer turns up for long enough, pauses it with a message
            # instead of pretending everything is fine.
            disabled = await self._record_failure_and_maybe_disable(
                p,
                scraper_name=scraper_name,
                domain=domain,
                reason="condition_mismatch",
                detail=f"offer is {info.condition!r}, tracking {p.preferred_condition!r}",
                collector=collector,
            )
            return (p.user_id, None, disabled, None)

        history = [h.price for h in await self.deps.repo.get_price_history(p.id, limit=50)]
        verdict = classify_read(info.price, history)

        if verdict is ReadVerdict.REJECT:
            logger.warning(
                "Product %d: read %s rejected as implausible (history_n=%d)",
                p.id,
                info.price,
                len(history),
            )
            if metrics is not None:
                metrics.outlier_rejected_total.labels(scraper=scraper_name, domain=domain).inc()
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="outlier_rejected"
                ).inc()
            # No confirmation count can make a scale-error magnitude believable,
            # so this reading is never accepted. Discarding it in silence would
            # leave the product reporting a stale price forever while looking
            # healthy — the same trap as the old outlier deadlock — so it is
            # recorded, surfaces in /errori, and eventually pauses the product.
            disabled = await self._record_failure_and_maybe_disable(
                p,
                scraper_name=scraper_name,
                domain=domain,
                reason="implausible_read",
                detail=f"{info.price} against a median of recent readings",
                collector=collector,
            )
            return (p.user_id, None, disabled, None)

        if verdict is ReadVerdict.CONFIRM and not await self._confirm_read(p, info.price):
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="awaiting_confirmation"
                ).inc()
            if domain != "unknown":
                await handle_success_in_pipeline(health_mgr=self.deps.health_mgr, domain=domain)
            await self.deps.repo.reset_errors(p.id)
            return (p.user_id, None, False, None)

        old_price = p.current_price or p.initial_price
        await self.deps.repo.update_price(p.id, info.price)
        await self.deps.repo.add_price_history(p.id, info.price)
        if p.pending_read_count or p.pending_read_streak:
            await self.deps.repo.clear_pending_read(p.id)
        await self.deps.repo.reset_errors(p.id)
        if domain != "unknown":
            await handle_success_in_pipeline(health_mgr=self.deps.health_mgr, domain=domain)
        if metrics is not None:
            metrics.price_check_total.labels(
                scraper=scraper_name, domain=domain, status="success"
            ).inc()

        came_back_in_stock = info.available and not p.is_available
        if info.available != p.is_available:
            await self.deps.repo.set_availability(p.id, available=info.available)
        if came_back_in_stock:
            # Routed with the product id so a restock obeys the same mute, quiet
            # hours and digest settings as a price drop — it is the same kind of
            # message to the user, and a mute that leaked restocks would be a
            # mute in name only.
            async with self._recipient_locale(p.user_id):
                text = format_back_in_stock(
                    product_name=p.name or p.url,
                    url=p.url,
                    price=info.price,
                    currency=p.currency,
                )
            await self._notify(
                p.user_id,
                text,
                product_id=p.id,
                payload={
                    "kind": "price",
                    "product_id": p.id,
                    "product_name": p.name or p.url,
                    "url": p.url,
                    "old_price": str(p.current_price) if p.current_price is not None else "",
                    "new_price": str(info.price),
                    "currency": p.currency,
                    "domain": domain,
                },
            )

        if old_price is None:
            return (p.user_id, None, False, None)
        threshold_type = cast("ThresholdType", p.threshold_type)
        threshold_hit = crosses_threshold(
            old=old_price,
            new=info.price,
            threshold_type=threshold_type,
            threshold_value=p.threshold_value,
        )
        # A target is a crossing, not a state: alert when the price moves from
        # above it to at-or-below it, so a product parked under its target does
        # not re-announce itself every cooldown window.
        target_hit = p.target_price is not None and info.price <= p.target_price < old_price
        if not (threshold_hit or target_hit):
            return (p.user_id, None, False, None)
        alert = PriceAlert(
            product_id=p.id,
            product_name=p.name or p.url,
            url=p.url,
            old_price=old_price,
            new_price=info.price,
            currency=p.currency,
            threshold_type=threshold_type,
            threshold_value=p.threshold_value,
        )
        return (p.user_id, alert, False, None)

    @staticmethod
    def _condition_matches(product: ProductRecord, scraped_condition: str | None) -> bool:
        """Return ``True`` when the scraped offer is the one the user tracks.

        Scrapers report the buy-box condition (``new`` / ``used`` / ``renewed``)
        when they can tell. If the user pinned a condition for this product, an
        offer in a different condition is a *different* product for pricing
        purposes: a warehouse deal appearing in the buy-box must not be recorded
        as the tracked item's price, let alone alerted on as a price drop.

        Silent when either side is unknown — most scrapers never populate the
        field, and the historical default is to track whatever the buy-box shows.
        """
        if product.preferred_condition is None or scraped_condition is None:
            return True
        return scraped_condition == product.preferred_condition

    async def _confirm_read(self, product: ProductRecord, price: Decimal) -> bool:
        """Hold an implausible read until a second, agreeing read backs it up.

        Returns ``True`` when ``price`` completes the required run of agreeing
        reads and may now be trusted; ``False`` when it has been parked and the
        caller must drop this check without persisting or alerting.

        This is what separates a transient bad scrape from a real repricing: a
        glitch does not repeat, a real price does. It also unwedges the opposite
        failure — a genuine level shift that history keeps rejecting — because a
        sustained new price confirms itself and is let through.
        """
        previous = product.pending_read_price
        streak = product.pending_read_streak + 1
        agreed = previous is not None and reads_agree(previous, price)
        confirmations = product.pending_read_count + 1 if agreed else 1

        if agreed and confirmations >= self.deps.read_confirmations:
            logger.info(
                "Product %d: implausible read %s confirmed by %d agreeing checks — accepting",
                product.id,
                price,
                confirmations,
            )
            return True

        if streak >= MAX_HELD_READS:
            logger.warning(
                "Product %d: %d consecutive implausible reads without agreement — "
                "rebaselining on %s rather than tracking a stale price forever",
                product.id,
                streak,
                price,
            )
            return True

        logger.info(
            "Product %d: implausible read %s held (agreeing run %d, held streak %d, previous %s)",
            product.id,
            price,
            confirmations,
            streak,
            previous,
        )
        await self.deps.repo.set_pending_read(product.id, price, confirmations, streak)
        return False

    async def _check_product(
        self,
        product_id: int,
        *,
        scraper_name: str = "unknown",
        domain: str = "unknown",
        collector: NoticeCollector,
    ) -> None:
        """Push-mode check used by the periodic job: scrape and dispatch via notifier.

        Operational notices are accumulated by
        :meth:`_record_failure_and_maybe_disable`; this wrapper only handles
        the price-drop alert path.
        """
        outcome = await self._check_product_core(
            product_id, scraper_name=scraper_name, domain=domain, collector=collector
        )
        if outcome is None:
            return
        user_id, alert, _disabled, _reason = outcome
        if alert is None:
            return
        # Anti-flap dedup: an oscillating price re-crosses the threshold on every
        # downswing. Suppress the repeat push so the user is notified once per
        # drop episode (re-notifying only on a new low or after the cooldown).
        product = await self.deps.repo.get_product(alert.product_id)
        if product is not None and self._is_duplicate_alert(product, new_price=alert.new_price):
            if self.deps.metrics is not None:
                self.deps.metrics.notification_skipped_total.labels(reason="cooldown").inc()
            return
        async with self._recipient_locale(user_id):
            text = format_alert(alert)
        if await self._notify(
            user_id,
            text,
            product_id=alert.product_id,
            payload=_alert_payload(alert, domain=domain),
        ):
            await self.deps.repo.record_alert_sent(alert.product_id, alert.new_price)

    def _is_duplicate_alert(
        self, product: ProductRecord, *, new_price: Decimal, now: datetime | None = None
    ) -> bool:
        """Return ``True`` when a price-drop alert is a repeat to be suppressed.

        A repeat is suppressed only when all of the following hold: a prior alert
        exists for the product (``last_notified_at`` and ``pending_alert_price``
        set), the new price is **not** a new low (``new_price >= pending_alert_price``),
        and the cooldown window has not yet elapsed. The first alert of an
        episode, a genuinely lower price (better deal), and an alert past the
        cooldown window are always allowed through.
        """
        last_at = product.last_notified_at
        last_price = product.pending_alert_price
        if last_at is None or last_price is None:
            return False
        if new_price < last_price:
            return False
        elapsed = (now or datetime.now(UTC)) - _parse_db_timestamp(last_at)
        return elapsed < timedelta(hours=self.deps.notification_cooldown_hours)

    async def check_products_for_user(
        self,
        *,
        product_ids: list[int],
        user_id: int,
        delay_between_products: float | None = None,
    ) -> list[CheckResult]:
        """Run the sole pull-mode loop and flush its isolated collector once."""
        collector = NoticeCollector()
        results: list[CheckResult] = []
        effective_delay = (
            self.deps.delay_between_products
            if delay_between_products is None
            else delay_between_products
        )
        half_open_seen: set[str] = set()
        try:
            for product_id in product_ids:
                product = await self.deps.repo.get_product_for_user(product_id, user_id)
                if product is None or not product.is_active:
                    continue
                domain = extract_etld_plus_one(product.url) or "unknown"
                if domain != "unknown":
                    if self.deps.health_mgr.is_locked(domain):
                        if self.deps.metrics is not None:
                            self.deps.metrics.quarantine_skip_total.labels(domain=domain).inc()
                        continue
                    if self.deps.health_mgr.is_half_open(domain):
                        if domain in half_open_seen:
                            continue
                        half_open_seen.add(domain)
                scraper = self.deps.registry.resolve(product.url)
                scraper_name = scraper.name if scraper is not None else "unknown"
                try:
                    outcome = await self._check_product_core(
                        product.id,
                        scraper_name=scraper_name,
                        domain=domain,
                        collector=collector,
                    )
                except BlockEvent as exc:
                    logger.warning("Block detected for product %d: %s", product.id, exc)
                    if self.deps.metrics is not None:
                        self.deps.metrics.price_check_total.labels(
                            scraper=scraper_name, domain=domain, status="block"
                        ).inc()
                    if domain != "unknown":
                        previous = self.deps.health_mgr.state(domain)
                        await handle_block_in_pipeline(
                            exc, health_mgr=self.deps.health_mgr, domain=domain
                        )
                        if previous == QuarantineState.CLOSED and self.deps.health_mgr.is_locked(
                            domain
                        ):
                            await self._notify_quarantine_entry(product, domain, reason=str(exc))
                    disabled = await self._record_failure_and_maybe_disable(
                        product,
                        scraper_name=scraper_name,
                        domain=domain,
                        reason="block",
                        detail=str(exc),
                        collector=collector,
                    )
                    results.append(
                        CheckResult(product.id, user_id, disabled=disabled, reason="block")
                    )
                except ListingGone as exc:
                    reason, detail = _failure_reason(exc)
                    disabled = await self._record_failure_and_maybe_disable(
                        product,
                        scraper_name=scraper_name,
                        domain=domain,
                        reason=reason,
                        detail=detail,
                        collector=collector,
                    )
                    results.append(
                        CheckResult(product.id, user_id, disabled=disabled, reason=reason)
                    )
                except ParseError as exc:
                    disabled = await self._record_failure_and_maybe_disable(
                        product,
                        scraper_name=scraper_name,
                        domain=domain,
                        reason="parse_error",
                        detail=str(exc),
                        collector=collector,
                    )
                    results.append(
                        CheckResult(product.id, user_id, disabled=disabled, reason="parse_error")
                    )
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    reason, detail = _failure_reason(exc)
                    disabled = await self._record_failure_and_maybe_disable(
                        product,
                        scraper_name=scraper_name,
                        domain=domain,
                        reason=reason,
                        detail=detail,
                        collector=collector,
                    )
                    results.append(
                        CheckResult(product.id, user_id, disabled=disabled, reason=reason)
                    )
                except Exception as exc:  # noqa: BLE001 — preserve pull-loop isolation
                    logger.exception("Unexpected error checking product %d: %s", product.id, exc)
                    try:
                        disabled = await self._record_failure_and_maybe_disable(
                            product,
                            scraper_name=scraper_name,
                            domain=domain,
                            reason="unexpected",
                            detail=str(exc),
                            collector=collector,
                        )
                    except Exception:  # noqa: BLE001 — failure bookkeeping is isolated too
                        logger.exception("Failed to record failure for product %d", product.id)
                        disabled = False
                    results.append(
                        CheckResult(product.id, user_id, disabled=disabled, reason="unexpected")
                    )
                else:
                    if outcome is None:
                        results.append(CheckResult(product.id, user_id))
                    else:
                        _outcome_user_id, alert, disabled, outcome_reason = outcome
                        results.append(
                            CheckResult(
                                product.id,
                                user_id,
                                alert=alert,
                                disabled=disabled,
                                reason=outcome_reason,
                            )
                        )
                await asyncio.sleep(effective_delay)
            return results
        finally:
            await self._flush_guaranteed(collector)

    async def check_one_product_for_user(self, *, product_id: int, user_id: int) -> CheckResult:
        """Delegate a single pull-mode check to the sole pull loop."""
        results = await self.check_products_for_user(
            product_ids=[product_id], user_id=user_id, delay_between_products=0
        )
        return results[0] if results else CheckResult(product_id=product_id, user_id=user_id)

    async def check_user_products_for_user(
        self, *, user_id: int, delay_between_products: float | None = None
    ) -> list[CheckResult]:
        """Delegate all active user products to the sole pull-mode loop."""
        products = await self.deps.repo.list_products_for_user(user_id=user_id, only_active=True)
        return await self.check_products_for_user(
            product_ids=[product.id for product in products],
            user_id=user_id,
            delay_between_products=delay_between_products,
        )
