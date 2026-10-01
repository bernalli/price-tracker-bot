"""Disabled browser fallback: Chromium cannot use the validated-IP transport."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from price_tracker.core.scraper_base import AbstractScraper, ProductInfo

if TYPE_CHECKING:
    import re

    import httpx


def available() -> bool:
    """Return False until all browser traffic can use validated connections."""
    return False


class PlaywrightFallbackScraper(AbstractScraper):
    """Retain registry compatibility without starting an unrestricted browser."""

    name: ClassVar[str] = "playwright_fallback"
    priority: ClassVar[int] = 10
    domain_patterns: ClassVar[list[re.Pattern[str]]] = []

    def can_handle(self, url: str) -> bool:
        """Let the generic HTTP scraper handle URLs while rendering is disabled."""
        return False

    async def scrape(self, url: str, client: httpx.AsyncClient | None = None) -> ProductInfo:
        """Refuse rendering, including direct calls when Chromium is installed."""
        return ProductInfo(error="Playwright rendering disabled: unbound browser connections")
