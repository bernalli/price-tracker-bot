"""Periodic scheduler sweeps, pacing, quarantine filtering, and due logic."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from price_tracker.core.exceptions import BlockEvent, ListingGone, ParseError
from price_tracker.core.health import QuarantineState
from price_tracker.core.notices import NoticeCollector
from price_tracker.core.scheduler_common import (
    CHECK_TICK_MINUTES,
    MAX_INTERVAL_MINUTES,
    SchedulerDeps,
    _failure_reason,
    _parse_db_timestamp,
)
from price_tracker.core.scraper_base import handle_block_in_pipeline
from price_tracker.core.url_utils import extract_etld_plus_one

if TYPE_CHECKING:
    from price_tracker.db.models import ProductRecord

logger = logging.getLogger("price_tracker.core.scheduler")


class PeriodicRunsMixin:
    """Periodic and single-user push-mode sweep responsibility."""

    deps: SchedulerDeps
    _attempted_at: dict[int, datetime]

    if TYPE_CHECKING:

        async def _check_product(
            self,
            product_id: int,
            *,
            scraper_name: str = "unknown",
            domain: str = "unknown",
            collector: NoticeCollector,
        ) -> None: ...

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

        async def _flush_guaranteed(self, collector: NoticeCollector) -> None: ...

    async def _scrape_one(self, product: ProductRecord, *, collector: NoticeCollector) -> None:
        """Scrape a single product and persist results (delegates to _check_product).

        Resolves scraper_name + domain at the top so that block/parse/error
        metric emissions all share the same labels regardless of where the
        exception is raised within the scrape pipeline. Failures are routed
        through :meth:`_record_failure_and_maybe_disable` so the product is
        auto-paused once the consecutive-error threshold is crossed.
        """
        domain = extract_etld_plus_one(product.url) or "unknown"
        scraper = self.deps.registry.resolve(product.url)
        scraper_name = scraper.name if scraper is not None else "unknown"
        metrics = self.deps.metrics
        try:
            await self._check_product(
                product.id, scraper_name=scraper_name, domain=domain, collector=collector
            )
        except BlockEvent as e:
            logger.warning("Block detected for product %d: %s", product.id, e)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="block"
                ).inc()
            if domain != "unknown":
                # Capture the pre-block state so we notify exactly once, on the
                # CLOSED → LOCKED transition (no spam while a domain stays locked).
                prev_state = self.deps.health_mgr.state(domain)
                await handle_block_in_pipeline(e, health_mgr=self.deps.health_mgr, domain=domain)
                if prev_state == QuarantineState.CLOSED and self.deps.health_mgr.is_locked(domain):
                    await self._notify_quarantine_entry(product, domain, reason=str(e))
            await self._record_failure_and_maybe_disable(
                product,
                scraper_name=scraper_name,
                domain=domain,
                reason="block",
                detail=str(e),
                collector=collector,
            )
        except ListingGone as e:
            logger.warning("Listing gone for product %d: %s", product.id, e)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            reason, detail = _failure_reason(e)
            await self._record_failure_and_maybe_disable(
                product,
                scraper_name=scraper_name,
                domain=domain,
                reason=reason,
                detail=detail,
                collector=collector,
            )
        except ParseError as e:
            logger.warning("Parse error for product %d: %s", product.id, e)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            await self._record_failure_and_maybe_disable(
                product,
                scraper_name=scraper_name,
                domain=domain,
                reason="parse_error",
                detail=str(e),
                collector=collector,
            )
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning("Check failed for product %d: %s", product.id, e)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            reason, detail = _failure_reason(e)
            await self._record_failure_and_maybe_disable(
                product,
                scraper_name=scraper_name,
                domain=domain,
                reason=reason,
                detail=detail,
                collector=collector,
            )
        except Exception as e:  # noqa: BLE001 — one product must never abort the sweep
            # Unexpected: a scraper leaking a non-contract exception, or a DB error
            # (e.g. sqlite 'database is locked' under tick/`/checkall` contention).
            # Isolate it to this product so the remaining sweep still runs.
            logger.exception("Unexpected error checking product %d: %s", product.id, e)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            try:
                await self._record_failure_and_maybe_disable(
                    product,
                    scraper_name=scraper_name,
                    domain=domain,
                    reason="unexpected",
                    detail=str(e),
                    collector=collector,
                )
            except Exception:  # noqa: BLE001 — bookkeeping must also not abort the sweep
                logger.exception(
                    "Failed to record failure for product %d after unexpected error", product.id
                )

    async def _run_tick(
        self,
        products: list[ProductRecord],
        *,
        half_open_seen: set[str] | None = None,
        collector: NoticeCollector,
    ) -> None:
        """One scheduler tick: scrape all eligible products.

        Filtering rules (domain quarantine):
          - skip products on LOCKED domains entirely
          - on HALF_OPEN domains send exactly one probe (first product per domain per tick)

        ``half_open_seen`` lets a caller share the probed-domain set across
        multiple ticks: ``run_check_all`` passes one set for the whole global
        sweep so a HALF_OPEN domain receives a single probe per sweep instead
        of one per user (#17). When ``None`` (single-user callers) a fresh set
        scoped to this tick is used.

        Rate-limiting pacing (`delay_between_products`) is applied between scrapes
        to be friendly to upstream servers.
        """
        metrics = self.deps.metrics
        if metrics is not None:
            metrics.scheduler_jobs_active.set(len(products))
        if half_open_seen is None:
            half_open_seen = set()
        for product in products:
            domain = extract_etld_plus_one(product.url)
            if not domain:
                # Unknown domain — best-effort scrape (Generic scraper handles it)
                await self._scrape_one(product, collector=collector)
                await asyncio.sleep(self.deps.delay_between_products)
                continue

            if self.deps.health_mgr.is_locked(domain):
                if metrics is not None:
                    metrics.quarantine_skip_total.labels(domain=domain).inc()
                continue  # skip — domain is in quarantine lockout; no sleep needed

            if self.deps.health_mgr.is_half_open(domain):
                if domain in half_open_seen:
                    continue  # only one probe per half-open domain per tick; no sleep needed
                half_open_seen.add(domain)

            await self._scrape_one(product, collector=collector)
            await asyncio.sleep(self.deps.delay_between_products)

    async def run_check_for_user(self, *, user_id: int) -> None:
        """Check every active product owned by `user_id` sequentially."""
        products = await self.deps.repo.list_products_for_user(user_id=user_id, only_active=True)
        collector = NoticeCollector()
        try:
            await self._run_tick(products, collector=collector)
        finally:
            await self._flush_guaranteed(collector)

    async def run_check_all(self) -> None:
        """Check every active product across every active user.

        A single ``half_open_seen`` set is shared across the per-user ticks so
        a HALF_OPEN domain is probed at most once per global sweep (#17).
        """
        users = await self.deps.repo.list_active_users()
        half_open_seen: set[str] = set()
        for u in users:
            products = await self.deps.repo.list_products_for_user(
                user_id=u.user_id, only_active=True
            )
            collector = NoticeCollector()
            try:
                await self._run_tick(products, half_open_seen=half_open_seen, collector=collector)
            finally:
                await self._flush_guaranteed(collector)

    def _last_attempt(self, product: ProductRecord) -> datetime | None:
        """The latest known attempt: this process's own, else the stored read or failure.

        A stored timestamp that cannot be read is ignored, so one damaged row makes
        its product due instead of stopping the periodic job for every product.
        """
        if product.id in self._attempted_at:
            return self._attempted_at[product.id]
        stored: list[datetime] = []
        for value in (product.last_checked_at, product.last_error_at):
            if not value:
                continue
            try:
                stored.append(_parse_db_timestamp(value))
            except (ValueError, TypeError, AttributeError):
                logger.warning("Unreadable stored timestamp for product %s: %r", product.id, value)
        return max(stored) if stored else None

    @staticmethod
    def _interval_minutes(product: ProductRecord, *, global_minutes: int) -> int:
        """The product's own interval if it is a usable one, else the global interval."""
        own = product.check_interval_minutes
        if isinstance(own, int) and not isinstance(own, bool) and 1 <= own <= MAX_INTERVAL_MINUTES:
            return own
        return min(max(global_minutes, CHECK_TICK_MINUTES), MAX_INTERVAL_MINUTES)

    def _is_due(self, product: ProductRecord, *, global_minutes: int, now: datetime) -> bool:
        """Whether the product's interval (its own, else the global one) has elapsed.

        Half a tick of slack keeps a product from slipping a whole tick late on
        every cycle when its interval is a multiple of the tick.
        """
        last = self._last_attempt(product)
        if last is None:
            return True
        interval = timedelta(minutes=self._interval_minutes(product, global_minutes=global_minutes))
        return now - last + timedelta(minutes=CHECK_TICK_MINUTES / 2) >= interval

    async def run_check_due(
        self, *, global_interval_minutes: int, now: datetime | None = None
    ) -> None:
        """Check, across every active user, the active products whose interval has elapsed.

        The periodic job calls this every ``CHECK_TICK_MINUTES``. A product's own
        ``check_interval_minutes`` overrides ``global_interval_minutes``; a failed
        read counts as an attempt, so a failing product is not retried every tick.
        """
        moment = now or datetime.now(UTC)
        users = await self.deps.repo.list_active_users()
        half_open_seen: set[str] = set()
        for u in users:
            products = await self.deps.repo.list_products_for_user(
                user_id=u.user_id, only_active=True
            )
            due = [
                p
                for p in products
                if self._is_due(p, global_minutes=global_interval_minutes, now=moment)
            ]
            if not due:
                continue
            collector = NoticeCollector()
            try:
                await self._run_tick(due, half_open_seen=half_open_seen, collector=collector)
            finally:
                await self._flush_guaranteed(collector)
