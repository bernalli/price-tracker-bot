"""Playwright headless fallback scraper.

Used when the primary scraper returns price=None. Renders the page with a
real Chromium so we capture JS-mounted prices on SPAs / Shopify variants /
sites that expose prices only after client-side hydration.

Soft-imports playwright — the module is still importable on containers
without Chromium. In that case `available()` returns False and the
scheduler skips this fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from bs4 import BeautifulSoup

from price_tracker.core.scraper_base import AbstractScraper, ProductInfo

if TYPE_CHECKING:
    import re

    import httpx

logger = logging.getLogger(__name__)


def available() -> bool:
    """Return True if playwright + chromium browser are both present."""
    try:
        import playwright  # noqa: F401
        from playwright.async_api import async_playwright  # noqa: F401
        return True
    except ImportError:
        return False


class PlaywrightFallbackScraper(AbstractScraper):
    """Headless Chromium fallback. Call only when the primary scraper failed."""

    name: ClassVar[str] = "playwright_fallback"
    priority: ClassVar[int] = 10
    domain_patterns: ClassVar[list[re.Pattern[str]]] = []

    def can_handle(self, url: str) -> bool:  # noqa: ARG002
        # Always handles — registry priority order keeps it after the
        # specific scrapers (Amazon=100, eBay=90, Shopify=80) and before the
        # generic catch-all (priority=0).
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient | None = None) -> ProductInfo:  # noqa: ARG002
        if not available():
            return ProductInfo(error="Playwright non disponibile")
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            return ProductInfo(error=f"Playwright import error: {e}")

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    locale="it-IT",
                )
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    # Give SPA frameworks a beat to mount price widgets.
                    await page.wait_for_timeout(2500)
                    html = await page.content()
                finally:
                    await context.close()
                    await browser.close()
        except (ValueError, OSError, RuntimeError) as e:
            logger.warning("Playwright render failed for %s: %s", url[:60], e)
            return ProductInfo(error=f"Playwright: {e}")

        soup = BeautifulSoup(html, "lxml")
        info = ProductInfo()

        # Re-use generic extraction strategies for maximum coverage.
        from price_tracker.scrapers.generic import GenericScraper

        generic = GenericScraper()
        for _name, fn in [
            ("JSON-LD", generic._try_json_ld),
            ("Microdata", generic._try_microdata),
            ("OpenGraph", generic._try_opengraph),
            ("Meta tags", generic._try_meta_tags),
            ("CSS selectors", generic._try_css_selectors),
            ("Data attributes", generic._try_data_attributes),
            ("JS product data", generic._try_js_product_data),
            ("Regex", generic._try_regex),
        ]:
            try:
                result = fn(soup)
                if result:
                    if result.get("price") is not None and info.price is None:
                        info.price = result["price"]
                    if result.get("name") and info.name is None:
                        info.name = result["name"]
                    if result.get("currency"):
                        info.currency = result["currency"]
                if info.price is not None and info.name is not None:
                    break
            except (ValueError, KeyError, AttributeError):
                continue

        if info.price is None:
            info.error = "Prezzo non trovato (anche via Playwright)"
        else:
            logger.info("Playwright fallback recovered price %s for %s", info.price, url[:60])
        return info
