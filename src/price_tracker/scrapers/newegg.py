"""Newegg scraper — extracts product info from newegg.com.

Strategy: DOM .product-price li.price-current strong (integer) + sup (decimals)
(primary) with JSON-LD Product fallback.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import ClassVar, TypedDict

import httpx
from bs4 import BeautifulSoup, Tag

from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_currency,
    get_headers,
    select_jsonld_offer,
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


class StrategyResult(TypedDict, total=False):
    price: Decimal
    currency: str
    name: str


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_newegg_html(url: str, client: httpx.AsyncClient) -> httpx.Response:
    headers = get_headers()
    response = await client.get(url, headers=headers, follow_redirects=True)
    return response


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
            response = await _fetch_newegg_html(url, client)
            detect_block_event(status_code=response.status_code, body=response.text, url=url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.debug("Newegg fetch failed for %s: %s", url[:80], e)
            return ProductInfo(error=f"HTTP error: {e}")

        soup = BeautifulSoup(response.text, "lxml")
        info = ProductInfo()

        for strategy_name, strategy_fn in (
            ("dom", self._try_dom),
            ("jsonld", self._try_jsonld),
        ):
            try:
                result = strategy_fn(soup)
                if not result:
                    continue
                if result.get("price") is not None and result["price"] > 0 and info.price is None:
                    info.price = result["price"]
                    logger.debug("newegg price via %s: %s", strategy_name, info.price)
                if result.get("currency") and info.currency is None:
                    info.currency = result["currency"]
                if result.get("name") and info.name is None:
                    info.name = result["name"]
                if info.price is not None and info.name is not None:
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

        if info.currency is None:
            info.currency = detect_currency(str(info.price or "")) or "USD"

        if info.price is None:
            info.error = "Price not found in page"

        return info

    def _try_dom(self, soup: BeautifulSoup) -> StrategyResult | None:
        current = soup.select_one(".product-price li.price-current")
        if not isinstance(current, Tag):
            return None
        strong_el = current.find("strong")
        if not isinstance(strong_el, Tag):
            return None
        integer_str = strong_el.get_text(strip=True).replace(",", "")
        if not integer_str:
            return None
        sup_el = current.find("sup")
        decimals_str = ""
        if isinstance(sup_el, Tag):
            decimals_str = sup_el.get_text(strip=True).lstrip(".").rstrip()
        combined = integer_str + ("." + decimals_str if decimals_str else "")
        try:
            parsed = Decimal(combined)
        except (InvalidOperation, ValueError):
            return None
        result: StrategyResult = {"price": parsed}

        symbol_el = current.select_one(".currency-symbol")
        if isinstance(symbol_el, Tag):
            symbol = symbol_el.get_text(strip=True)
            if symbol:
                result["currency"] = _symbol_to_iso(symbol)
        return result

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
                    "currency": currency or "USD",
                }
                name = item.get("name")
                if isinstance(name, str) and name:
                    result["name"] = name[:200]
                return result
        return None
