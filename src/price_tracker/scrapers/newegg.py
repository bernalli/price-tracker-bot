"""Newegg scraper — extracts product info from newegg.com.

Strategy: DOM .product-price li.price-current strong (integer) + sup (decimals)
(primary) with JSON-LD Product fallback.
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar

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


StrategyResult = EvidenceResult


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_newegg_html(url: str, client: httpx.AsyncClient) -> str:
    headers = get_headers()
    page = await fetch_page(url, client, headers=headers)
    return ImagePageHTML(page.text, page.url_final)


class NeweggScraper(AbstractScraper):
    """Scraper for Newegg product pages."""

    name: ClassVar[str] = "newegg"
    priority: ClassVar[int] = 50
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"^([\w-]+\.)?newegg\.com$", re.IGNORECASE),
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        try:
            html = await _fetch_newegg_html(url, client)
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("Newegg fetch failed for %s: %s", url[:80], e)
            return ProductInfo(stock_evidence=False, error=f"HTTP error: {e}")

        soup = BeautifulSoup(html, "lxml")
        if not page_matches(soup, url, getattr(html, "final_url", url)):
            return ProductInfo(stock_evidence=False, error="identity_mismatch")
        info = ProductInfo(stock_evidence=False)
        unknown_currency = False

        for strategy_name, strategy_fn in (
            ("jsonld", lambda page: self._try_jsonld(page, url)),
            ("dom", self._try_dom),
        ):
            try:
                result = strategy_fn(soup)
                if result and result.get("identity_mismatch"):
                    return ProductInfo(stock_evidence=False, error="identity_mismatch")
                if result and result.get("currency_ambiguous"):
                    return ProductInfo(stock_evidence=False, error="currency_ambiguous")
                if result and result.get("available") is False:
                    return ProductInfo(
                        name=result.get("name"), currency=result.get("currency"), available=False
                    )
                if not result:
                    continue
                if result.get("name") and info.name is None:
                    info.name = result["name"]
                if result.get("price") is None:
                    continue
                if not result.get("currency"):
                    unknown_currency = True
                    continue
                # Price, unit and stock form one observation; a later strategy
                # may replace an incomplete quote, never complete its fields.
                info.price = result["price"]
                info.currency = result["currency"]
                info.stock_evidence = result.get("available") is not None
                if info.stock_evidence:
                    info.available = result["available"]
                logger.debug("newegg price via %s: %s", strategy_name, info.price)
                break
            except (ValueError, KeyError, AttributeError) as e:
                logger.debug("newegg strategy %s error: %s", strategy_name, e)
                continue

        if info.name is None:
            title_tag = soup.find("title")
            if isinstance(title_tag, Tag):
                title_text = title_tag.get_text(strip=True)
                if title_text:
                    info.name = title_text[:200]

        if info.price is None and unknown_currency:
            return ProductInfo(name=info.name, stock_evidence=False, error="currency_ambiguous")

        if info.price is None:
            info.error = "Price not found in page"

        return info

    def _try_jsonld(self, soup: BeautifulSoup, url: str) -> StrategyResult | None:
        return product_evidence(soup, url)

    def _try_dom(self, soup: BeautifulSoup) -> StrategyResult | None:
        current = soup.select_one(".product-price li.price-current")
        if not isinstance(current, Tag):
            return None
        strong_el = current.find("strong")
        if not isinstance(strong_el, Tag):
            return None
        # Keep the entire owned price context: reconstructing only strong/sup
        # discarded qualifiers such as /month or a second range endpoint.
        return dom_evidence(current.get_text("", strip=True))
