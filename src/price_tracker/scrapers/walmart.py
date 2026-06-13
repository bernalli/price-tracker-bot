"""Walmart scraper — extracts product info from walmart.com.

Strategy: JSON-LD Product/offers (primary) with itemprop=price microdata fallback.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, ClassVar, TypedDict

import httpx
from bs4 import BeautifulSoup, Tag

if TYPE_CHECKING:
    from decimal import Decimal

from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_currency,
    find_microdata_price_el,
    get_headers,
    parse_price,
    select_jsonld_offer,
)

logger = logging.getLogger(__name__)


class StrategyResult(TypedDict, total=False):
    """Optional fields a strategy can populate."""

    price: Decimal
    currency: str
    name: str


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_walmart_html(url: str, client: httpx.AsyncClient) -> httpx.Response:
    headers = get_headers()
    response = await client.get(url, headers=headers, follow_redirects=True)
    return response


class WalmartScraper(AbstractScraper):
    """Scraper for Walmart product pages."""

    name: ClassVar[str] = "walmart"
    priority: ClassVar[int] = 50
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"^([\w-]+\.)?walmart\.com$", re.IGNORECASE),
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        try:
            response = await _fetch_walmart_html(url, client)
            detect_block_event(status_code=response.status_code, body=response.text, url=url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.debug("Walmart fetch failed for %s: %s", url[:80], e)
            return ProductInfo(error=f"HTTP error: {e}")

        soup = BeautifulSoup(response.text, "lxml")
        info = ProductInfo()

        for strategy_name, strategy_fn in (
            ("jsonld", self._try_jsonld),
            ("microdata", self._try_microdata),
        ):
            try:
                result = strategy_fn(soup)
                if not result:
                    continue
                if result.get("price") is not None and result["price"] > 0 and info.price is None:
                    info.price = result["price"]
                    logger.debug("walmart price via %s: %s", strategy_name, info.price)
                if result.get("currency") and info.currency is None:
                    info.currency = result["currency"]
                if result.get("name") and info.name is None:
                    info.name = result["name"]
                if info.price is not None and info.name is not None:
                    break
            except (ValueError, KeyError, AttributeError) as e:
                logger.debug("walmart strategy %s error: %s", strategy_name, e)
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

    def _try_microdata(self, soup: BeautifulSoup) -> StrategyResult | None:
        price_el = find_microdata_price_el(soup)
        if not isinstance(price_el, Tag):
            return None
        raw = price_el.get("content") or price_el.get_text(strip=True)
        parsed = parse_price(str(raw))
        if parsed is None:
            return None
        result: StrategyResult = {"price": parsed}
        # Scope currency/name to the price element's own itemscope (Offer for the
        # currency, enclosing Product for the name): a page-wide find would pick
        # up a related-items carousel's values (#37). Flat markup without any
        # itemscope keeps the page-wide lookup.
        offer_scope = price_el.find_parent(attrs={"itemscope": True})
        currency_root: Tag | BeautifulSoup = offer_scope if isinstance(offer_scope, Tag) else soup
        name_root: Tag | BeautifulSoup = soup
        if isinstance(offer_scope, Tag):
            product_scope = offer_scope.find_parent(attrs={"itemscope": True})
            name_root = product_scope if isinstance(product_scope, Tag) else offer_scope
        currency_el = currency_root.find(attrs={"itemprop": "priceCurrency"})
        if isinstance(currency_el, Tag):
            currency_val = currency_el.get("content") or currency_el.get_text(strip=True)
            if currency_val:
                result["currency"] = str(currency_val)
        name_el = name_root.find(attrs={"itemprop": "name"})
        if isinstance(name_el, Tag):
            name_val = name_el.get("content") or name_el.get_text(strip=True)
            if name_val:
                result["name"] = str(name_val)[:200]
        return result
