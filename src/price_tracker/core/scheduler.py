"""Scheduler — periodic price check + threshold alert dispatch."""

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
from price_tracker.core.health import HealthManager
from price_tracker.core.outlier import is_outlier
from price_tracker.core.url_utils import extract_etld_plus_one

if TYPE_CHECKING:
    from price_tracker.core.registry import ScraperRegistry
    from price_tracker.db.models import ProductRecord
    from price_tracker.db.repository import Repository

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


class Scheduler:
    """Runs a price check sweep over all active products."""

    def __init__(self, deps: SchedulerDeps) -> None:
        self.deps = deps

    async def _scrape_one(self, product: ProductRecord) -> None:
        """Scrape a single product and persist results (delegates to _check_product)."""
        try:
            await self._check_product(product.id)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning("Check failed for product %d: %s", product.id, e)
            await self.deps.repo.increment_errors(product.id)

    async def _run_tick(self, products: list[ProductRecord]) -> None:
        """One scheduler tick: scrape all eligible products.

        Filtering rules per Feature B:
          - skip products on LOCKED domains entirely
          - on HALF_OPEN domains send exactly one probe (first product per domain per tick)

        Rate-limiting pacing (`delay_between_products`) is applied between scrapes
        to be friendly to upstream servers.
        """
        half_open_seen: set[str] = set()
        for product in products:
            domain = extract_etld_plus_one(product.url)
            if not domain:
                # Unknown domain — best-effort scrape (Generic scraper handles it)
                await self._scrape_one(product)
                await asyncio.sleep(self.deps.delay_between_products)
                continue

            if self.deps.health_mgr.is_locked(domain):
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

    async def _check_product(self, product_id: int) -> None:
        p = await self.deps.repo.get_product(product_id)
        if p is None or not p.is_active:
            return

        scraper = self.deps.registry.resolve(p.url)
        if scraper is None:
            logger.warning("No scraper for %s", p.url)
            return

        info = await scraper.scrape(p.url, self.deps.client)
        if info.price is None:
            await self.deps.repo.increment_errors(p.id)
            return

        # Outlier check against price history
        history = [h.price for h in await self.deps.repo.get_price_history(p.id, limit=50)]
        outlier = is_outlier(info.price, history)
        if outlier.is_outlier:
            logger.warning(
                "Product %d: OUTLIER read %s rejected (median=%s, ratio=%s, history_n=%d)",
                p.id,
                info.price,
                outlier.median,
                outlier.ratio,
                outlier.history_n,
            )
            return

        old_price = p.current_price or p.initial_price
        await self.deps.repo.update_price(p.id, info.price)
        await self.deps.repo.add_price_history(p.id, info.price)
        await self.deps.repo.reset_errors(p.id)

        if old_price is None:
            return
        threshold_type = cast("ThresholdType", p.threshold_type)
        if crosses_threshold(
            old=old_price,
            new=info.price,
            threshold_type=threshold_type,
            threshold_value=p.threshold_value,
        ):
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
            await self.deps.notifier(p.user_id, format_alert(alert))

    async def cleanup_old_history(self, *, retention_days: int = 365) -> int:
        """Delete price_history rows older than `retention_days`. Returns row count."""
        return await self.deps.repo.delete_old_price_history(days=retention_days)
