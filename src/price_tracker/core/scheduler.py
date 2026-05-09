"""Scheduler — periodic price check + threshold alert dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import httpx

from price_tracker.core.alert import (
    PriceAlert,
    ThresholdType,
    crosses_threshold,
    format_alert,
)
from price_tracker.core.outlier import is_outlier

if TYPE_CHECKING:
    from price_tracker.core.registry import ScraperRegistry
    from price_tracker.db.repository import Repository

logger = logging.getLogger(__name__)

# Notifier coroutine: receives a user_id and a formatted alert message.
NotifierFn = Callable[[int, str], Awaitable[None]]


@dataclass
class SchedulerDeps:
    """Dependencies bundle for the Scheduler."""

    repo: Repository
    registry: ScraperRegistry
    client: httpx.AsyncClient
    notifier: NotifierFn
    max_consecutive_errors: int = 10
    delay_between_products: float = 5.0


class Scheduler:
    """Runs a price check sweep over all active products."""

    def __init__(self, deps: SchedulerDeps) -> None:
        self.deps = deps

    async def run_check_for_user(self, *, user_id: int) -> None:
        """Check every active product owned by `user_id` sequentially."""
        products = await self.deps.repo.list_products_for_user(user_id=user_id, only_active=True)
        for p in products:
            try:
                await self._check_product(p.id)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                logger.warning("Check failed for product %d: %s", p.id, e)
                await self.deps.repo.increment_errors(p.id)
            await asyncio.sleep(self.deps.delay_between_products)

    async def run_check_all(self) -> None:
        """Check every active product across every active user."""
        users = await self.deps.repo.list_active_users()
        for u in users:
            await self.run_check_for_user(user_id=u.user_id)

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
