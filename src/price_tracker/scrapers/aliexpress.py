"""AliExpress scraper — extracts product info from aliexpress.* (7 TLDs).

Strategy: window.runParams JS blob (primary) — parsed via regex from raw HTML
before BeautifulSoup. DOM .product-price-value + .product-price-currency
fallback runs through the standard strategy loop.
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
    jsonld_offer_availability,
    parse_price,
)

if TYPE_CHECKING:
    from decimal import Decimal

logger = logging.getLogger(__name__)

_SYMBOL_TO_ISO: dict[str, str] = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₽": "RUB",
}


def _symbol_to_iso(symbol: str) -> str:
    return _SYMBOL_TO_ISO.get(symbol.strip(), "USD")


class StrategyResult(TypedDict, total=False):
    price: Decimal
    currency: str
    name: str
    available: bool


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_aliexpress_html(url: str, client: httpx.AsyncClient) -> httpx.Response:
    headers = get_headers()
    response = await client.get(url, headers=headers, follow_redirects=True)
    response.raise_for_status()
    return response


_RUNPARAMS_RE = re.compile(r"window\.runParams\s*=\s*(\{.*?\});", re.DOTALL)


class AliexpressScraper(AbstractScraper):
    """Scraper for AliExpress product pages across major locales."""

    name: ClassVar[str] = "aliexpress"
    priority: ClassVar[int] = 50
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"^([\w-]+\.)?aliexpress\.(com|it|fr|de|es|ru|nl)$", re.IGNORECASE),
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        try:
            response = await _fetch_aliexpress_html(url, client)
            detect_block_event(status_code=response.status_code, body=response.text, url=url)
        except httpx.HTTPStatusError as e:
            # raise_for_status() lives inside the retry scope now, so hard
            # blocks (403/429) surface here — re-check them for BlockEvent
            # before degrading, so quarantine still engages (#55).
            detect_block_event(status_code=e.response.status_code, body=e.response.text, url=url)
            logger.debug("AliExpress fetch failed for %s: %s", url[:80], e)
            return ProductInfo(error=f"HTTP error: {e}")
        except httpx.HTTPError as e:
            logger.debug("AliExpress fetch failed for %s: %s", url[:80], e)
            return ProductInfo(error=f"HTTP error: {e}")

        info = ProductInfo()

        # Primary: window.runParams JS blob (parsed before soup construction)
        try:
            runparams_result = self._try_runparams(response.text)
            if runparams_result and runparams_result.get("price") is not None:
                info.price = runparams_result["price"]
                if runparams_result.get("currency"):
                    info.currency = runparams_result["currency"]
                if runparams_result.get("name"):
                    info.name = runparams_result["name"]
                logger.debug("aliexpress price via runparams: %s", info.price)
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError, KeyError) as e:
            logger.debug("aliexpress runparams strategy error: %s", e)

        soup = BeautifulSoup(response.text, "lxml")

        # Fallback strategies (skipped once price+name are populated)
        for strategy_name, strategy_fn in (
            ("jsonld", self._try_jsonld),
            ("dom", self._try_dom),
        ):
            if info.price is not None and info.name is not None:
                break
            try:
                result = strategy_fn(soup)
                if result and result.get("price") is not None and info.price is None:
                    info.price = result["price"]
                    if result.get("currency") and info.currency is None:
                        info.currency = result["currency"]
                    if result.get("name") and info.name is None:
                        info.name = result["name"]
                    if result.get("available") is not None:
                        info.available = result["available"]
                    logger.debug("aliexpress price via %s: %s", strategy_name, info.price)
                elif result and result.get("name") and info.name is None:
                    info.name = result["name"]
            except (ValueError, KeyError, AttributeError) as e:
                logger.debug("aliexpress strategy %s error: %s", strategy_name, e)
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

    def _try_runparams(self, html: str) -> StrategyResult | None:
        m = _RUNPARAMS_RE.search(html)
        if not m:
            return None
        try:
            blob = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(blob, dict):
            return None
        data = blob.get("data") if isinstance(blob.get("data"), dict) else {}
        pc = data.get("priceComponent") if isinstance(data, dict) else None
        if not isinstance(pc, dict):
            return None
        discounted = pc.get("discountedPrice") or pc.get("origPrice") or {}
        if not isinstance(discounted, dict):
            return None
        amt = discounted.get("minActivityAmount") or discounted.get("minAmount") or {}
        if not isinstance(amt, dict):
            return None
        price = parse_price(str(amt.get("value", "")))
        if price is None or price <= 0:
            return None
        currency = str(amt.get("currency") or "USD")
        result: StrategyResult = {"price": price, "currency": currency}
        # Try to grab name from the same blob (subject)
        subject = data.get("titleModule", {}) if isinstance(data, dict) else {}
        if isinstance(subject, dict):
            name = subject.get("subject")
            if isinstance(name, str) and name:
                result["name"] = name[:200]
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
                result: StrategyResult = {}
                offers = item.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                if isinstance(offers, dict):
                    price_raw = offers.get("price") or offers.get("lowPrice")
                    if price_raw is not None:
                        parsed = parse_price(str(price_raw))
                        if parsed is not None:
                            result["price"] = parsed
                            result["currency"] = str(offers.get("priceCurrency", "USD"))
                            availability = jsonld_offer_availability(offers)
                            if availability is not None:
                                result["available"] = availability
                name = item.get("name")
                if isinstance(name, str) and name:
                    result["name"] = name[:200]
                if result:
                    return result
        return None

    def _try_dom(self, soup: BeautifulSoup) -> StrategyResult | None:
        value_el = soup.select_one(".product-price-value")
        if not isinstance(value_el, Tag):
            return None
        text = value_el.get_text(strip=True)
        parsed = parse_price(text)
        if parsed is None:
            return None
        result: StrategyResult = {"price": parsed}
        currency_el = soup.select_one(".product-price-currency")
        if isinstance(currency_el, Tag):
            currency_text = currency_el.get_text(strip=True)
            if currency_text:
                # currency may be a symbol or an ISO code
                if currency_text in _SYMBOL_TO_ISO:
                    result["currency"] = _symbol_to_iso(currency_text)
                else:
                    result["currency"] = currency_text[:3].upper()
        return result
