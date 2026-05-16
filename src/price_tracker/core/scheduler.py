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
from typing import TYPE_CHECKING, cast

import httpx

from price_tracker.core.alert import (
    PriceAlert,
    ThresholdType,
    crosses_threshold,
    format_alert,
)
from price_tracker.core.exceptions import BlockEvent, ParseError
from price_tracker.core.health import HealthManager
from price_tracker.core.outlier import is_outlier
from price_tracker.core.url_utils import extract_etld_plus_one

if TYPE_CHECKING:
    from price_tracker.core.registry import ScraperRegistry
    from price_tracker.db.models import ProductRecord
    from price_tracker.db.repository import Repository
    from price_tracker.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)

# Notifier coroutine: receives a user_id and a formatted alert message.
NotifierFn = Callable[[int, str], Awaitable[None]]


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
    health_mgr: HealthManager = field(default_factory=_no_op_health_mgr)
    metrics: MetricsRegistry | None = None


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one product check in pull mode.

    Returned by ``check_one_product_for_user`` and ``check_user_products_for_user``
    so interactive handlers can render their own response. ``alert`` is set only
    when the new price actually crossed the threshold.
    """

    product_id: int
    user_id: int
    alert: PriceAlert | None = None


class Scheduler:
    """Runs a price check sweep over all active products."""

    def __init__(self, deps: SchedulerDeps) -> None:
        self.deps = deps

    async def _scrape_one(self, product: ProductRecord) -> None:
        """Scrape a single product and persist results (delegates to _check_product).

        Resolves scraper_name + domain at the top so that block/parse/error
        metric emissions all share the same labels regardless of where the
        exception is raised within the scrape pipeline.
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
            await self.deps.repo.increment_errors(product.id)
        except ParseError as e:
            logger.warning("Parse error for product %d: %s", product.id, e)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            await self.deps.repo.increment_errors(product.id)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning("Check failed for product %d: %s", product.id, e)
            if metrics is not None:
                metrics.price_check_total.labels(
                    scraper=scraper_name, domain=domain, status="error"
                ).inc()
            await self.deps.repo.increment_errors(product.id)

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

    async def _check_product_core(
        self,
        product_id: int,
        *,
        scraper_name: str = "unknown",
        domain: str = "unknown",
    ) -> tuple[int, PriceAlert] | None:
        """Scrape one product, persist, and return ``(user_id, alert)`` if the
        threshold fired. Otherwise return ``None``.

        Side-effects: writes price/history/errors to the repository and emits
        metrics. Does **not** invoke ``deps.notifier`` — the caller decides
        whether to push (periodic job) or accumulate (interactive handler).
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
            return None

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
            await self.deps.repo.increment_errors(p.id)
            return None

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
            return None

        old_price = p.current_price or p.initial_price
        await self.deps.repo.update_price(p.id, info.price)
        await self.deps.repo.add_price_history(p.id, info.price)
        await self.deps.repo.reset_errors(p.id)
        if metrics is not None:
            metrics.price_check_total.labels(
                scraper=scraper_name, domain=domain, status="success"
            ).inc()

        if old_price is None:
            return None
        threshold_type = cast("ThresholdType", p.threshold_type)
        if not crosses_threshold(
            old=old_price,
            new=info.price,
            threshold_type=threshold_type,
            threshold_value=p.threshold_value,
        ):
            return None
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
        return (p.user_id, alert)

    async def _check_product(
        self,
        product_id: int,
        *,
        scraper_name: str = "unknown",
        domain: str = "unknown",
    ) -> None:
        """Push-mode check used by the periodic job: scrape and dispatch via notifier."""
        outcome = await self._check_product_core(
            product_id, scraper_name=scraper_name, domain=domain
        )
        if outcome is None:
            return
        user_id, alert = outcome
        await self.deps.notifier(user_id, format_alert(alert))

    async def check_one_product_for_user(self, *, product_id: int, user_id: int) -> CheckResult:
        """Pull-mode single-product check used by ``/check`` and the per-product
        "Check now" inline button.

        The product is scraped through the same pipeline used by the periodic
        job (outlier rejection, health-manager events, metrics) but the
        resulting alert — if any — is returned to the caller instead of being
        pushed to Telegram. ``user_id`` is recorded on the result so the caller
        can verify ownership when needed.
        """
        outcome = await self._check_product_core(product_id)
        if outcome is None:
            return CheckResult(product_id=product_id, user_id=user_id, alert=None)
        _, alert = outcome
        return CheckResult(product_id=product_id, user_id=user_id, alert=alert)

    async def check_user_products_for_user(self, *, user_id: int) -> list[CheckResult]:
        """Pull-mode batch check used by ``/checkall`` and the menu "Check all" button.

        Iterates over every active product owned by ``user_id``, respecting the
        same per-tick rate-limiting and domain quarantine rules as
        ``_run_tick`` (so a quarantined domain is skipped silently rather than
        scraped). Returns one :class:`CheckResult` per attempted product so the
        caller can build a summary message inline.
        """
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
                await self.deps.repo.increment_errors(product.id)
                results.append(CheckResult(product_id=product.id, user_id=user_id, alert=None))
            except ParseError as e:
                logger.warning("Parse error for product %d: %s", product.id, e)
                if metrics is not None:
                    metrics.price_check_total.labels(
                        scraper=scraper_name, domain=domain, status="error"
                    ).inc()
                await self.deps.repo.increment_errors(product.id)
                results.append(CheckResult(product_id=product.id, user_id=user_id, alert=None))
            except (httpx.HTTPError, ValueError, KeyError) as e:
                logger.warning("Check failed for product %d: %s", product.id, e)
                if metrics is not None:
                    metrics.price_check_total.labels(
                        scraper=scraper_name, domain=domain, status="error"
                    ).inc()
                await self.deps.repo.increment_errors(product.id)
                results.append(CheckResult(product_id=product.id, user_id=user_id, alert=None))
            else:
                alert = outcome[1] if outcome is not None else None
                results.append(CheckResult(product_id=product.id, user_id=user_id, alert=alert))
            await asyncio.sleep(self.deps.delay_between_products)
        return results

    async def cleanup_old_history(self, *, retention_days: int = 365) -> int:
        """Delete price_history rows older than `retention_days`. Returns row count."""
        return await self.deps.repo.delete_old_price_history(days=retention_days)
