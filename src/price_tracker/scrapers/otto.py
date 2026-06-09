"""Otto scraper — extracts product info from otto.de.

Strategy: JSON-LD Product/offers (primary) with DOM .pdp_price__main-price fallback.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, ClassVar, TypedDict

import httpx
from bs4 import BeautifulSoup, Tag

from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_currency,
    get_headers,
    parse_price,
    select_jsonld_offer,
)

if TYPE_CHECKING:
    from decimal import Decimal

logger = logging.getLogger(__name__)


class StrategyResult(TypedDict, total=False):
    price: Decimal
    currency: str
    name: str


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_otto_html(url: str, client: httpx.AsyncClient) -> httpx.Response:
    headers = get_headers()
    response = await client.get(url, headers=headers, follow_redirects=True)
    return response


class OttoScraper(AbstractScraper):
    """Scraper for otto.de product pages."""

    name: ClassVar[str] = "otto"
    priority: ClassVar[int] = 50
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"^([\w-]+\.)?otto\.de$", re.IGNORECASE),
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        try:
            response = await _fetch_otto_html(url, client)
            detect_block_event(status_code=response.status_code, body=response.text, url=url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.debug("Otto fetch failed for %s: %s", url[:80], e)
            return ProductInfo(error=f"HTTP error: {e}")

        soup = BeautifulSoup(response.text, "lxml")
        info = ProductInfo()

        for strategy_name, strategy_fn in (
            ("jsonld", self._try_jsonld),
            ("dom", self._try_dom),
        ):
            try:
                result = strategy_fn(soup)
                if result and result.get("price") is not None and info.price is None:
                    info.price = result["price"]
                    if result.get("currency") and info.currency is None:
                        info.currency = result["currency"]
                    if result.get("name") and info.name is None:
                        info.name = result["name"]
                    logger.debug("otto price via %s: %s", strategy_name, info.price)
                    if info.price is not None and info.name is not None:
                        break
            except (ValueError, KeyError, AttributeError) as e:
                logger.debug("otto strategy %s error: %s", strategy_name, e)
                continue

        if info.name is None:
            title_tag = soup.find("title")
            if isinstance(title_tag, Tag):
                title_text = title_tag.get_text(strip=True)
                if title_text:
                    info.name = title_text[:200]

        if info.currency is None:
            info.currency = detect_currency(str(info.price or "")) or "EUR"

        if info.price is None:
            info.error = "Price not found in page"

        return info

    def _try_jsonld(self, soup: BeautifulSoup) -> StrategyResult | None:
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            raw = script.string or script.get_text(strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                type_val = item.get("@type", "")
                type_str = " ".join(type_val) if isinstance(type_val, list) else str(type_val)
                if "Product" not in type_str:
                    continue
                selected = select_jsonld_offer(item.get("offers"))
                if selected is None:
                    continue
                parsed, currency = selected
                result: StrategyResult = {
                    "price": parsed,
                    "currency": currency or "EUR",
                }
                name = item.get("name")
                if isinstance(name, str) and name:
                    result["name"] = name[:200]
                return result
        return None

    def _try_dom(self, soup: BeautifulSoup) -> StrategyResult | None:
        el = soup.select_one(".pdp_price__main-price")
        if not isinstance(el, Tag):
            return None
        text = el.get_text(strip=True)
        parsed = parse_price(text)
        if parsed is None:
            return None
        result: StrategyResult = {"price": parsed}
        if "€" in text or "EUR" in text.upper():
            result["currency"] = "EUR"
        return result
