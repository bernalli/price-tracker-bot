"""Scheduler — periodic price check + threshold alert dispatch.

Two dispatch modes:

* **Push (default)** — used by the periodic ``run_check_all`` job. After scraping
  a product the scheduler hands the alert to ``deps.notifier`` together with the
  product id and its structured payload, so a preference-aware notifier can
  mute, defer or digest it instead of sending immediately.
* **Pull (interactive handlers)** — ``check_one_product_for_user`` and
  ``check_user_products_for_user`` accumulate :class:`CheckResult` objects and
  return them to the caller, which renders its own summary message
  (``/check``, ``/checkall``, menu/product callbacks). The notifier is **not**
  invoked in pull mode — the handler is responsible for the user reply.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

from price_tracker.core.alert import (
    PriceAlert,
    ThresholdType,
    crosses_threshold,
    format_alert,
    format_back_in_stock,
    format_error_notification,
    format_quarantine_notification,
)
from price_tracker.core.exceptions import BlockEvent, ParseError
from price_tracker.core.health import HealthManager, QuarantineState
from price_tracker.core.outlier import (
    MAX_HELD_READS,
    REQUIRED_CONFIRMATIONS,
    ReadVerdict,
    classify_read,
    reads_agree,
)
from price_tracker.core.scraper_base import (
    handle_block_in_pipeline,
    handle_success_in_pipeline,
)
from price_tracker.core.url_utils import extract_etld_plus_one

if TYPE_CHECKING:
    from decimal import Decimal

    from price_tracker.core.registry import ScraperRegistry
    from price_tracker.db.models import ProductRecord
    from price_tracker.db.repository import Repository
    from price_tracker.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)


class NotifierFn(Protocol):
    """Delivers one formatted message to one user.

    ``product_id`` and ``payload`` are optional so operational notices
    (auto-disable, quarantine) can be sent without them; a notifier that honours
    per-product notification preferences uses them to route price alerts.

    Returning ``False`` means the message was NOT delivered and nothing took
    responsibility for it, so the scheduler must not record it as sent.
    ``None`` is the legacy "fire and forget" answer and counts as delivered, so
    older notifiers keep working unchanged.
    """

    async def __call__(
        self,
        user_id: int,
        text: str,
        *,
        product_id: int | None = ...,
        payload: dict[str, Any] | None = ...,
    ) -> bool | None: ...


def _alert_payload(alert: PriceAlert, *, domain: str) -> dict[str, Any]:
    """Structured view of an alert, for notifiers that do more than send text.

    The digest renders its lines from these fields, so they must survive the
    trip even when the immediate message body is pre-rendered.
    """
    return {
        "product_id": alert.product_id,
        "product_name": alert.product_name,
        "url": alert.url,
        "old_price": str(alert.old_price),
        "new_price": str(alert.new_price),
        "currency": alert.currency,
        "domain": domain,
    }


def _parse_db_timestamp(value: str) -> datetime:
    """Parse a DB timestamp (SQLite ``datetime('now')`` or ISO-8601) as UTC-aware.

    Stored notification timestamps come from ``datetime('now')``
    (``"YYYY-MM-DD HH:MM:SS"``, naive UTC); legacy rows migrated from v2 may use
    ISO-8601 with a trailing ``Z``. Both are normalized to a UTC-aware datetime.
    """
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _no_op_health_mgr() -> HealthManager:
    """Return a HealthManager subclass that never locks or half-opens anything."""
    from price_tracker.core.health import QuarantineState  # local import avoids circularity

    class _NoOpHealthManager(HealthManager):
        def __init__(self) -> None:
            pass  # skip Repository dependency

        def state(self, domain: str) -> QuarantineState:  # noqa: ARG002
            return QuarantineState.CLOSED

        def is_locked(self, domain: str) -> bool:  # noqa: ARG002
            return False

        def is_half_open(self, domain: str) -> bool:  # noqa: ARG002
            return False

        async def record_block(self, domain: str, *, reason: str) -> QuarantineState:  # noqa: ARG002
            return QuarantineState.CLOSED

        async def record_success(self, domain: str) -> QuarantineState:  # noqa: ARG002
            return QuarantineState.CLOSED

    return _NoOpHealthManager()


@dataclass
class SchedulerDeps:
    """Dependencies bundle for the Scheduler."""

    repo: Repository
    registry: ScraperRegistry
    client: httpx.AsyncClient
    notifier: NotifierFn
    max_consecutive_errors: int = 10
    delay_between_products: float = 5.0
    notification_cooldown_hours: int = 24
    health_mgr: HealthManager = field(default_factory=_no_op_health_mgr)
    metrics: MetricsRegistry | None = None
    read_confirmations: int = REQUIRED_CONFIRMATIONS
    """Agreeing reads needed before an implausible price is trusted.

    Costs ``(read_confirmations - 1) × check_interval`` of latency on a genuine
    steep discount, so deployments checking every few hours may prefer a lower
    value than the default.
    """


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one product check in pull mode.

    Returned by ``check_one_product_for_user`` and ``check_user_products_for_user``
    so interactive handlers can render their own response. ``alert`` is set only
    when the new price actually crossed the threshold. ``disabled`` is True when
    this tick brought ``consecutive_errors`` to ``max_consecutive_errors`` and the
    product was auto-paused — the handler can flag that in the summary message.
    """

    product_id: int
    user_id: int
    alert: PriceAlert | None = None
    disabled: bool = False


class Scheduler:
    """Runs a price check sweep over all active products."""

    def __init__(self, deps: SchedulerDeps) -> None:
        self.deps = deps

    async def _scrape_one(self, product: ProductRecord) -> None:
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
            await self._check_product(product.id, scraper_name=scraper_name, domain=domain)
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
                product, scraper_name=scraper_name, domain=domain, reason="block", detail=str(e)
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
            )
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning("Check failed for product %d: %s", product.id, e)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            await self._record_failure_and_maybe_disable(
                product,
                scraper_name=scraper_name,
                domain=domain,
                reason="http_error",
                detail=str(e),
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
    ) -> None:
        """One scheduler tick: scrape all eligible products.

        Filtering rules per Feature B:
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
                await self._scrape_one(product)
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

            await self._scrape_one(product)
            await asyncio.sleep(self.deps.delay_between_products)

    async def run_check_for_user(self, *, user_id: int) -> None:
        """Check every active product owned by `user_id` sequentially."""
        products = await self.deps.repo.list_products_for_user(user_id=user_id, only_active=True)
        await self._run_tick(products)

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
            await self._run_tick(products, half_open_seen=half_open_seen)

    async def _notify_quarantine_entry(
        self, product: ProductRecord, domain: str, *, reason: str
    ) -> None:
        """Push a one-shot alert when ``domain`` first enters quarantine.

        Called only on the CLOSED → LOCKED transition. The notifier runs under a
        broad try/except so a flaky transport never aborts the scheduler tick.
        """
        message = format_quarantine_notification(
            domain=domain,
            reason=reason,
            locked_until=self.deps.health_mgr.locked_until(domain),
        )
        try:
            await self.deps.notifier(product.user_id, message)
        except Exception:  # noqa: BLE001 — notifier failure must not kill the tick
            logger.exception(
                "Notifier failed to deliver quarantine alert for domain %s (user %d)",
                domain,
                product.user_id,
            )

    async def _record_failure_and_maybe_disable(
        self,
        product: ProductRecord,
        *,
        scraper_name: str,
        domain: str,
        reason: str,
        detail: str | None = None,
    ) -> bool:
        """Increment ``consecutive_errors`` and auto-disable on threshold.

        Always increments the error count. Re-reads the product to obtain the
        updated counter (so concurrent ticks see a consistent value), and when
        the counter reaches ``deps.max_consecutive_errors`` it:

        * pauses the product via :meth:`Repository.deactivate_product`
        * pushes one ``Tracking suspended`` notification to the owner via
          ``deps.notifier`` so users get a persistent record of the suspension
          even when the failure was detected during an interactive ``/checkall``.

        The notifier is invoked under a broad try/except: a flaky transport
        must not abort the surrounding scheduler tick. Returns ``True`` when
        the product was disabled *by this call* so pull-mode callers can flag
        the disabled status on their :class:`CheckResult`.

        ``scraper_name`` and ``domain`` are passed through for structured
        logging only. ``reason`` (plus optional ``detail``) is persisted as the
        product's ``last_error`` so the /errori command can surface it.
        """
        await self.deps.repo.increment_errors(product.id)
        await self.deps.repo.set_last_error(product.id, f"{reason}: {detail}" if detail else reason)
        # A check that failed produced no evidence about the price, so it breaks
        # any confirmation run in progress: two sightings either side of an
        # outage are not consecutive readings of the same claim.
        if product.pending_read_count or product.pending_read_streak:
            await self.deps.repo.clear_pending_read(product.id)
        updated = await self.deps.repo.get_product(product.id)
        if updated is None:
            return False
        if updated.consecutive_errors < self.deps.max_consecutive_errors:
            return False
        await self.deps.repo.deactivate_product(product.id)
        logger.warning(
            "Product %d auto-disabled after %d consecutive errors "
            "(scraper=%s, domain=%s, reason=%s)",
            product.id,
            updated.consecutive_errors,
            scraper_name,
            domain,
            reason,
        )
        message = format_error_notification(
            product={
                "name": product.name or product.url,
                "url": product.url,
            },
            error_count=updated.consecutive_errors,
            max_errors=self.deps.max_consecutive_errors,
        )
        try:
            await self.deps.notifier(product.user_id, message)
        except Exception:  # noqa: BLE001 — notifier failure must not kill the tick
            logger.exception(
                "Notifier failed to deliver auto-disable alert for product %d (user %d)",
                product.id,
                product.user_id,
            )
        return True

    async def _check_product_core(
        self,
        product_id: int,
        *,
        scraper_name: str = "unknown",
        domain: str = "unknown",
    ) -> tuple[int, PriceAlert | None, bool] | None:
        """Scrape one product, persist, and return ``(user_id, alert, disabled)``.

        * ``alert`` is set only when the new price actually crossed the threshold.
        * ``disabled`` is ``True`` when this call brought ``consecutive_errors``
          to ``max_consecutive_errors`` and the product was auto-paused.
        * Returns ``None`` when the product is missing or already inactive.

        Side-effects: writes price/history/errors to the repository and emits
        metrics. The notifier is invoked **only** by
        :meth:`_record_failure_and_maybe_disable` for auto-disable alerts;
        price-drop alerts are returned to the caller, which decides whether to
        push (periodic job) or accumulate (interactive handler).
        """
        p = await self.deps.repo.get_product(product_id)
        if p is None or not p.is_active:
            return None

        scraper = self.deps.registry.resolve(p.url)
        if scraper is None:
            logger.warning("No scraper for %s", p.url)
            metrics = self.deps.metrics
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            disabled = await self._record_failure_and_maybe_disable(
                p, scraper_name=scraper_name, domain=domain, reason="no_scraper"
            )
            return (p.user_id, None, disabled)

        metrics = self.deps.metrics
        if metrics is not None:
            with metrics.scraper_duration_seconds.labels(
                scraper=scraper_name, domain=domain
            ).time():
                info = await scraper.scrape(p.url, self.deps.client)
        else:
            info = await scraper.scrape(p.url, self.deps.client)

        if info.price is None:
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            disabled = await self._record_failure_and_maybe_disable(
                p, scraper_name=scraper_name, domain=domain, reason="price_none"
            )
            return (p.user_id, None, disabled)

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
            return (p.user_id, None, False)

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
            )
            return (p.user_id, None, disabled)

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
            )
            return (p.user_id, None, disabled)

        if verdict is ReadVerdict.CONFIRM and not await self._confirm_read(p, info.price):
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="awaiting_confirmation"
                ).inc()
            if domain != "unknown":
                await handle_success_in_pipeline(health_mgr=self.deps.health_mgr, domain=domain)
            return (p.user_id, None, False)

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
            await self._notify(
                p.user_id,
                format_back_in_stock(
                    product_name=p.name or p.url,
                    url=p.url,
                    price=info.price,
                    currency=p.currency,
                ),
                product_id=p.id,
                payload={
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
            return (p.user_id, None, False)
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
            return (p.user_id, None, False)
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
        return (p.user_id, alert, False)

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
    ) -> None:
        """Push-mode check used by the periodic job: scrape and dispatch via notifier.

        Auto-disable notifications are pushed inside
        :meth:`_record_failure_and_maybe_disable`; this wrapper only handles
        the price-drop alert path.
        """
        outcome = await self._check_product_core(
            product_id, scraper_name=scraper_name, domain=domain
        )
        if outcome is None:
            return
        user_id, alert, _disabled = outcome
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
        if await self._notify(
            user_id,
            format_alert(alert),
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

    async def check_one_product_for_user(self, *, product_id: int, user_id: int) -> CheckResult:
        """Pull-mode single-product check used by ``/check`` and the per-product
        "Check now" inline button.

        The product is scraped through the same pipeline used by the periodic
        job (outlier rejection, health-manager events, metrics) but the
        resulting alert — if any — is returned to the caller instead of being
        pushed to Telegram. ``user_id`` is recorded on the result so the caller
        can verify ownership when needed. ``disabled`` is propagated from
        :meth:`_check_product_core` so the handler can flag the auto-pause.
        """
        outcome = await self._check_product_core(product_id)
        if outcome is None:
            return CheckResult(product_id=product_id, user_id=user_id, alert=None)
        _, alert, disabled = outcome
        return CheckResult(product_id=product_id, user_id=user_id, alert=alert, disabled=disabled)

    async def check_user_products_for_user(
        self, *, user_id: int, delay_between_products: float | None = None
    ) -> list[CheckResult]:
        """Pull-mode batch check used by ``/checkall`` and the menu "Check all" button.

        Iterates over every active product owned by ``user_id``, respecting the
        same per-tick rate-limiting and domain quarantine rules as
        ``_run_tick`` (so a quarantined domain is skipped silently rather than
        scraped). Returns one :class:`CheckResult` per attempted product so the
        caller can build a summary message inline.

        ``delay_between_products`` overrides the per-product pause. The push
        mode (periodic job) leaves it unset and inherits the gentle
        ``deps.delay_between_products`` (default 5s) to be polite to upstream
        servers. Interactive callers (``/checkall``, menu button) override
        with a small value (≈0.5s) since the user is waiting in real time —
        gentleness still matters but the UX gap matters more.
        """
        effective_delay = (
            delay_between_products
            if delay_between_products is not None
            else self.deps.delay_between_products
        )
        products = await self.deps.repo.list_products_for_user(user_id=user_id, only_active=True)
        results: list[CheckResult] = []
        half_open_seen: set[str] = set()
        for product in products:
            domain = extract_etld_plus_one(product.url) or "unknown"

            if domain != "unknown":
                if self.deps.health_mgr.is_locked(domain):
                    metrics = self.deps.metrics
                    if metrics is not None:
                        metrics.quarantine_skip_total.labels(domain=domain).inc()
                    continue
                if self.deps.health_mgr.is_half_open(domain):
                    if domain in half_open_seen:
                        continue
                    half_open_seen.add(domain)

            scraper = self.deps.registry.resolve(product.url)
            scraper_name = scraper.name if scraper is not None else "unknown"
            metrics = self.deps.metrics
            try:
                outcome = await self._check_product_core(
                    product.id, scraper_name=scraper_name, domain=domain
                )
            except BlockEvent as e:
                logger.warning("Block detected for product %d: %s", product.id, e)
                if metrics is not None:
                    metrics.price_check_total.labels(
                        scraper=scraper_name, domain=domain, status="block"
                    ).inc()
                if domain != "unknown":
                    await handle_block_in_pipeline(
                        e, health_mgr=self.deps.health_mgr, domain=domain
                    )
                disabled = await self._record_failure_and_maybe_disable(
                    product, scraper_name=scraper_name, domain=domain, reason="block"
                )
                results.append(
                    CheckResult(
                        product_id=product.id,
                        user_id=user_id,
                        alert=None,
                        disabled=disabled,
                    )
                )
            except ParseError as e:
                logger.warning("Parse error for product %d: %s", product.id, e)
                if metrics is not None:
                    metrics.price_check_total.labels(
                        scraper=scraper_name, domain=domain, status="error"
                    ).inc()
                disabled = await self._record_failure_and_maybe_disable(
                    product, scraper_name=scraper_name, domain=domain, reason="parse_error"
                )
                results.append(
                    CheckResult(
                        product_id=product.id,
                        user_id=user_id,
                        alert=None,
                        disabled=disabled,
                    )
                )
            except (httpx.HTTPError, ValueError, KeyError) as e:
                logger.warning("Check failed for product %d: %s", product.id, e)
                if metrics is not None:
                    metrics.price_check_total.labels(
                        scraper=scraper_name, domain=domain, status="error"
                    ).inc()
                disabled = await self._record_failure_and_maybe_disable(
                    product, scraper_name=scraper_name, domain=domain, reason="http_error"
                )
                results.append(
                    CheckResult(
                        product_id=product.id,
                        user_id=user_id,
                        alert=None,
                        disabled=disabled,
                    )
                )
            except Exception as e:  # noqa: BLE001 — one product must never abort /checkall
                logger.exception("Unexpected error checking product %d: %s", product.id, e)
                if metrics is not None:
                    metrics.price_check_total.labels(
                        scraper=scraper_name, domain=domain, status="error"
                    ).inc()
                try:
                    disabled = await self._record_failure_and_maybe_disable(
                        product, scraper_name=scraper_name, domain=domain, reason="unexpected"
                    )
                except Exception:  # noqa: BLE001 — bookkeeping must also not abort the sweep
                    logger.exception(
                        "Failed to record failure for product %d after unexpected error",
                        product.id,
                    )
                    disabled = False
                results.append(
                    CheckResult(
                        product_id=product.id,
                        user_id=user_id,
                        alert=None,
                        disabled=disabled,
                    )
                )
            else:
                if outcome is None:
                    results.append(CheckResult(product_id=product.id, user_id=user_id, alert=None))
                else:
                    _, alert, disabled = outcome
                    results.append(
                        CheckResult(
                            product_id=product.id,
                            user_id=user_id,
                            alert=alert,
                            disabled=disabled,
                        )
                    )
            await asyncio.sleep(effective_delay)
        return results

    async def cleanup_old_history(self, *, retention_days: int = 365) -> int:
        """Delete price_history rows older than `retention_days`. Returns row count."""
        return await self.deps.repo.delete_old_price_history(days=retention_days)
