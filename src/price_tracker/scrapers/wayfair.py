"""Wayfair scraper — extracts product info from wayfair.com / .co.uk / .ca / .de.

Strategy: DOM [data-enzyme-id=PriceBlock] [data-test-id=PriceDisplay] (primary)
with JSON-LD Product fallback.
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from price_tracker.core.fetch import fetch_page
from price_tracker.core.page_image import ImagePageHTML
from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    get_headers,
)
from price_tracker.scrapers.secondary_evidence import (
    EvidenceResult,
    dom_evidence,
    page_matches,
    product_evidence,
)

logger = logging.getLogger(__name__)

_SYMBOL_TO_ISO: dict[str, str] = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "C$": "CAD",
    "CA$": "CAD",
}


def _symbol_to_iso(symbol: str) -> str:
    return _SYMBOL_TO_ISO.get(symbol.strip(), "USD")


def _default_currency_for_url(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if netloc.endswith(".co.uk"):
        return "GBP"
    if netloc.endswith(".de"):
        return "EUR"
    if netloc.endswith(".ca"):
        return "CAD"
    return "USD"


StrategyResult = EvidenceResult


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_wayfair_html(url: str, client: httpx.AsyncClient) -> str:
    headers = get_headers()
    page = await fetch_page(url, client, headers=headers)
    return ImagePageHTML(page.text, page.url_final)


class WayfairScraper(AbstractScraper):
    """Scraper for Wayfair product pages across major locales."""

    name: ClassVar[str] = "wayfair"
    priority: ClassVar[int] = 50
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"^([\w-]+\.)?wayfair\.(com|co\.uk|ca|de)$", re.IGNORECASE),
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        try:
            html = await _fetch_wayfair_html(url, client)
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("Wayfair fetch failed for %s: %s", url[:80], e)
            return ProductInfo(error=f"HTTP error: {e}", stock_evidence=False)

        soup = BeautifulSoup(html, "lxml")
        if not page_matches(soup, url, getattr(html, "final_url", url)):
            return ProductInfo(error="identity_mismatch", stock_evidence=False)
        info = ProductInfo(stock_evidence=False)

        for strategy_name, strategy_fn in (
            ("jsonld", lambda page: self._try_jsonld(page, url)),
            ("dom", self._try_dom),
        ):
            try:
                result = strategy_fn(soup)
                if result and result.get("identity_mismatch"):
                    return ProductInfo(error="identity_mismatch", stock_evidence=False)
                if result and result.get("currency_ambiguous"):
                    return ProductInfo(error="currency_ambiguous", stock_evidence=False)
                if result and result.get("available") is False:
                    return ProductInfo(
                        name=result.get("name"), currency=result.get("currency"), available=False
                    )
                if result and result.get("price") is not None and info.price is None:
                    info.price = result["price"]
                    if isinstance(result.get("available"), bool):
                        info.available = result["available"]
                        info.stock_evidence = True
                    if result.get("currency") and info.currency is None:
                        info.currency = result["currency"]
                    if result.get("name") and info.name is None:
                        info.name = result["name"]
                    logger.debug("wayfair price via %s: %s", strategy_name, info.price)
                    if info.price is not None and info.name is not None:
                        break
            except (ValueError, KeyError, AttributeError) as e:
                logger.debug("wayfair strategy %s error: %s", strategy_name, e)
                continue

        if info.name is None:
            title_tag = soup.find("title")
            if isinstance(title_tag, Tag):
                title_text = title_tag.get_text(strip=True)
                if title_text:
                    info.name = title_text[:200]

        if info.price is not None and info.currency is None:
            return ProductInfo(name=info.name, error="currency_ambiguous", stock_evidence=False)

        if info.price is None:
            info.error = "Price not found in page"

        return info

    def _try_jsonld(self, soup: BeautifulSoup, url: str) -> StrategyResult | None:
        return product_evidence(soup, url)

    def _try_dom(self, soup: BeautifulSoup) -> StrategyResult | None:
        block = soup.select_one('[data-enzyme-id="PriceBlock"]')
        if not isinstance(block, Tag):
            return None
        display = block.select_one('[data-test-id="PriceDisplay"]')
        if not isinstance(display, Tag):
            return None
        text = display.get_text(strip=True)
        return dom_evidence(text)
