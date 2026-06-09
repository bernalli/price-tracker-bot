"""Scheduler — periodic price check + threshold alert dispatch.

Two dispatch modes:

* **Push (default)** — used by the periodic ``run_check_all`` job. After scraping
  a product the scheduler invokes ``deps.notifier(user_id, formatted_text)`` so
  the configured Telegram notifier ships the message immediately.
* **Pull (interactive handlers)** — ``check_one_product_for_user`` and
  ``check_user_products_for_user`` accumulate :class:`CheckResult` objects and
  return them to the caller, which renders its own summary message
  (``/check``, ``/checkall``, menu/product callbacks). The notifier is **not**
  invoked in pull mode — the handler is responsible for the user reply.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import httpx

from price_tracker.core.alert import (
    PriceAlert,
    ThresholdType,
    crosses_threshold,
    format_alert,
    format_error_notification,
)
from price_tracker.core.exceptions import BlockEvent, ParseError
from price_tracker.core.health import HealthManager
from price_tracker.core.outlier import is_outlier
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

# Notifier coroutine: receives a user_id and a formatted alert message.
NotifierFn = Callable[[int, str], Awaitable[None]]


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
                await handle_block_in_pipeline(e, health_mgr=self.deps.health_mgr, domain=domain)
            await self._record_failure_and_maybe_disable(
                product, scraper_name=scraper_name, domain=domain, reason="block"
            )
        except ParseError as e:
            logger.warning("Parse error for product %d: %s", product.id, e)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            await self._record_failure_and_maybe_disable(
                product, scraper_name=scraper_name, domain=domain, reason="parse_error"
            )
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning("Check failed for product %d: %s", product.id, e)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            await self._record_failure_and_maybe_disable(
                product, scraper_name=scraper_name, domain=domain, reason="http_error"
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
                    product, scraper_name=scraper_name, domain=domain, reason="unexpected"
                )
            except Exception:  # noqa: BLE001 — bookkeeping must also not abort the sweep
                logger.exception(
                    "Failed to record failure for product %d after unexpected error", product.id
                )

    async def _run_tick(self, products: list[ProductRecord]) -> None:
        """One scheduler tick: scrape all eligible products.

        Filtering rules per Feature B:
          - skip products on LOCKED domains entirely
          - on HALF_OPEN domains send exactly one probe (first product per domain per tick)

        Rate-limiting pacing (`delay_between_products`) is applied between scrapes
        to be friendly to upstream servers.
        """
        metrics = self.deps.metrics
        if metrics is not None:
            metrics.scheduler_jobs_active.set(len(products))
        half_open_seen: set[str] = set()
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
        """Check every active product across every active user."""
        users = await self.deps.repo.list_active_users()
        for u in users:
            products = await self.deps.repo.list_products_for_user(
                user_id=u.user_id, only_active=True
            )
            await self._run_tick(products)

    async def _record_failure_and_maybe_disable(
        self,
        product: ProductRecord,
        *,
        scraper_name: str,
        domain: str,
        reason: str,
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

        ``scraper_name``, ``domain`` and ``reason`` are passed through for
        structured logging only — they are not persisted.
        """
        await self.deps.repo.increment_errors(product.id)
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

        history = [h.price for h in await self.deps.repo.get_price_history(p.id, limit=50)]
        outlier = is_outlier(
            info.price,
            history,
            metrics=metrics,
            scraper=scraper_name,
            domain=domain,
        )
        if outlier.is_outlier:
            logger.warning(
                "Product %d: OUTLIER read %s rejected (median=%s, ratio=%s, history_n=%d)",
                p.id,
                info.price,
                outlier.median,
                outlier.ratio,
                outlier.history_n,
            )
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="outlier_rejected"
                ).inc()
            return (p.user_id, None, False)

        old_price = p.current_price or p.initial_price
        await self.deps.repo.update_price(p.id, info.price)
        await self.deps.repo.add_price_history(p.id, info.price)
        await self.deps.repo.reset_errors(p.id)
        if domain != "unknown":
            await handle_success_in_pipeline(health_mgr=self.deps.health_mgr, domain=domain)
        if metrics is not None:
            metrics.price_check_total.labels(
                scraper=scraper_name, domain=domain, status="success"
            ).inc()

        if old_price is None:
            return (p.user_id, None, False)
        threshold_type = cast("ThresholdType", p.threshold_type)
        if not crosses_threshold(
            old=old_price,
            new=info.price,
            threshold_type=threshold_type,
            threshold_value=p.threshold_value,
        ):
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
        await self.deps.notifier(user_id, format_alert(alert))
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
