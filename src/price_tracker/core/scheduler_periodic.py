"""Periodic scheduler sweeps, store spacing, quarantine filtering, and due logic."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from price_tracker.core.domain_spacing import retry_after_of, run_spaced
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
    from collections.abc import Sequence

    from price_tracker.db.models import ProductRecord

logger = logging.getLogger("price_tracker.core.scheduler")


@dataclass(frozen=True, slots=True)
class _SweepJob:
    """One product check of a sweep, with the collector of its owner."""

    product: ProductRecord
    collector: NoticeCollector
    domain: str | None


def _tally(counts: Counter[str], stores: list[str] | None = None) -> str:
    """Render ``" (a=1, b=2: store.example)"`` for the sweep summary; empty when nothing counted.

    Only outcome names and store domains: no product name or URL reaches the log.
    """
    if not counts:
        return ""
    parts = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    return f" ({parts}: {', '.join(stores)})" if stores else f" ({parts})"


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
        ) -> str: ...

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

    async def _scrape_one(self, product: ProductRecord, *, collector: NoticeCollector) -> str:
        """Scrape a single product and persist results (delegates to _check_product).

        Resolves scraper_name + domain at the top so that block/parse/error
        metric emissions all share the same labels regardless of where the
        exception is raised within the scrape pipeline. Failures are routed
        through :meth:`_record_failure_and_maybe_disable` so the product is
        auto-paused once the consecutive-error threshold is crossed.

        Returns the outcome for the sweep summary: ``"ok"`` or the reason.
        """
        domain = extract_etld_plus_one(product.url) or "unknown"
        scraper = self.deps.registry.resolve(product.url)
        scraper_name = scraper.name if scraper is not None else "unknown"
        metrics = self.deps.metrics
        reason = "unexpected"
        try:
            return await self._check_product(
                product.id, scraper_name=scraper_name, domain=domain, collector=collector
            )
        except BlockEvent as e:
            reason = "block"
            logger.warning("Block detected for product %d: %s", product.id, e)
            self._defer_on_retry_after(domain, e)
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
            reason = "parse_error"
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
            self._defer_on_retry_after(domain, e)
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
        return reason

    def _defer_on_retry_after(self, domain: str, exc: BaseException) -> None:
        """Defer ``domain`` when the failed check's 429/503 carried ``Retry-After``."""
        if domain != "unknown":
            self.deps.domain_spacing.defer(domain, retry_after_of(exc))

    async def _run_tick(
        self,
        products: list[ProductRecord],
        *,
        half_open_seen: set[str] | None = None,
        collector: NoticeCollector,
    ) -> None:
        """One scheduler tick for one collector; the caller flushes it.

        See :meth:`_run_sweep` for the filtering and spacing rules.
        """
        await self._run_sweep(
            [(products, collector)],
            half_open_seen=half_open_seen,
            flush=False,
        )

    async def _run_sweep(
        self,
        batches: Sequence[tuple[Sequence[ProductRecord], NoticeCollector]],
        *,
        half_open_seen: set[str] | None = None,
        flush: bool = True,
    ) -> None:
        """Scrape the products of every batch in one interleaved run.

        Filtering rules (domain quarantine), applied right before each check:
          - skip products on LOCKED domains entirely
          - on HALF_OPEN domains send exactly one probe (first product per domain)

        Spacing (``core.domain_spacing``): two checks on the same store start at
        least ``DOMAIN_MIN_GAP_SECONDS`` apart, across batches (users) too, and a
        store waiting for its gap never holds back a product of another store.
        A store deferred by ``Retry-After`` is left for a later sweep: its
        products are not attempted, so they stay due.

        ``half_open_seen`` lets a caller share the probed-domain set across
        sweeps: a HALF_OPEN domain receives a single probe per sweep instead of
        one per user (#17). When ``None`` a fresh set scoped to this sweep is used.

        Rate-limiting pacing (`delay_between_products`) is applied after each
        scrape to be friendly to upstream servers.

        With ``flush`` each batch's collector is flushed once all its products are
        done or skipped (and, at the latest, when the sweep ends).
        """
        metrics = self.deps.metrics
        jobs = [
            _SweepJob(product, collector, extract_etld_plus_one(product.url) or None)
            for products, collector in batches
            for product in products
        ]
        if metrics is not None:
            metrics.scheduler_jobs_active.set(len(jobs))
        if half_open_seen is None:
            half_open_seen = set()
        remaining: dict[int, int] = {}
        for job in jobs:
            remaining[id(job.collector)] = remaining.get(id(job.collector), 0) + 1
        unflushed: dict[int, NoticeCollector] = {
            id(collector): collector for _products, collector in batches
        }

        async def settle(job: _SweepJob) -> None:
            key = id(job.collector)
            remaining[key] -= 1
            if flush and remaining[key] == 0 and key in unflushed:
                await self._flush_guaranteed(unflushed.pop(key))

        dropped: list[_SweepJob] = []
        started = time.monotonic()
        checked: Counter[str] = Counter()
        skipped: Counter[str] = Counter()
        paused_stores: set[str] = set()

        async def record_skip(job: _SweepJob) -> None:
            reason = (
                "quarantined"
                if job.domain and self.deps.health_mgr.is_locked(job.domain)
                else "probe_skipped"
                if job.domain and self.deps.health_mgr.is_half_open(job.domain)
                else "deferred"
            )
            skipped[reason] += 1
            if job.domain and reason != "deferred":
                paused_stores.add(job.domain)
            await self.deps.repo.record_check_outcome(job.product.id, reason=reason)

        def admit(job: _SweepJob, domain: str) -> bool:
            if self.deps.health_mgr.is_locked(domain):
                if metrics is not None:
                    metrics.quarantine_skip_total.labels(domain=domain).inc()
                return False  # skip — domain is in quarantine lockout
            # only one probe per half-open domain per sweep
            return not (self.deps.health_mgr.is_half_open(domain) and domain in half_open_seen)

        async def run(job: _SweepJob) -> None:
            for waiting in dropped:
                await record_skip(waiting)
                await settle(waiting)
            dropped.clear()
            if job.domain is not None and self.deps.health_mgr.is_half_open(job.domain):
                half_open_seen.add(job.domain)
            outcome = await self._scrape_one(job.product, collector=job.collector)
            checked[outcome if isinstance(outcome, str) else "ok"] += 1
            await settle(job)
            await asyncio.sleep(self.deps.delay_between_products)

        def drop(job: _SweepJob) -> None:
            if job.domain is not None and self.deps.domain_spacing.is_deferred(job.domain):
                logger.info(
                    "Product %d: %s asked to retry later; left for a later sweep",
                    job.product.id,
                    job.domain,
                )
            dropped.append(job)

        try:
            if flush:
                for key in [k for k in unflushed if remaining.get(k, 0) == 0]:
                    await self._flush_guaranteed(unflushed.pop(key))
            await run_spaced(
                jobs,
                spacing=self.deps.domain_spacing,
                domain_of=lambda job: job.domain,
                admit=admit,
                run=run,
                drop=drop,
            )
        finally:
            for job in dropped:
                await record_skip(job)
            if jobs:
                logger.info(
                    "Sweep done in %.1fs: %d checked%s; %d skipped%s",
                    time.monotonic() - started,
                    sum(checked.values()),
                    _tally(checked),
                    sum(skipped.values()),
                    _tally(skipped, sorted(paused_stores)),
                )
            if flush:
                cancelled = False
                for collector in list(unflushed.values()):
                    try:
                        await self._flush_guaranteed(collector)
                    except asyncio.CancelledError:
                        cancelled = True
                unflushed.clear()
                if cancelled:
                    raise asyncio.CancelledError

    async def run_check_for_user(self, *, user_id: int) -> None:
        """Check every active product owned by `user_id`, spaced per store."""
        products = await self.deps.repo.list_products_for_user(user_id=user_id, only_active=True)
        await self._run_sweep([(products, NoticeCollector())])

    async def run_check_all(self) -> None:
        """Check every active product across every active user.

        All users' products run in one interleaved sweep, so the per-store
        spacing holds across users and a HALF_OPEN domain is probed at most
        once per global sweep (#17).
        """
        users = await self.deps.repo.list_active_users()
        batches: list[tuple[Sequence[ProductRecord], NoticeCollector]] = []
        for u in users:
            products = await self.deps.repo.list_products_for_user(
                user_id=u.user_id, only_active=True
            )
            batches.append((products, NoticeCollector()))
        await self._run_sweep(batches)
        if not batches:
            await self._flush_guaranteed(NoticeCollector())

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
        batches: list[tuple[Sequence[ProductRecord], NoticeCollector]] = []
        for u in users:
            products = await self.deps.repo.list_products_for_user(
                user_id=u.user_id, only_active=True
            )
            due = [
                p
                for p in products
                if self._is_due(p, global_minutes=global_interval_minutes, now=moment)
            ]
            if due:
                batches.append((due, NoticeCollector()))
        await self._run_sweep(batches)
        if not batches:
            await self._flush_guaranteed(NoticeCollector())
