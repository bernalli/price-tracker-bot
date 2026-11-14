"""AliExpress scraper — extracts product info from aliexpress.* (7 TLDs).

Strategy: window.runParams JS blob (primary) — parsed via regex from raw HTML
before BeautifulSoup. DOM .product-price-value + .product-price-currency
fallback runs through the standard strategy loop.
"""

from __future__ import annotations

import json
import logging
import re
from typing import ClassVar

import httpx
from bs4 import BeautifulSoup, Tag

from price_tracker.core.http_client import public_request
from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_listing_gone,
    get_headers,
    parse_price,
    retry_after_header,
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
    "₽": "RUB",
}


def _symbol_to_iso(symbol: str) -> str:
    return _SYMBOL_TO_ISO.get(symbol.strip(), "USD")


StrategyResult = EvidenceResult


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_aliexpress_html(url: str, client: httpx.AsyncClient) -> httpx.Response:
    headers = get_headers()
    response = await public_request(client, "GET", url, headers=headers)
    detect_listing_gone(status_code=response.status_code, url=url)
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
            detect_block_event(
                status_code=response.status_code,
                body=response.text,
                url=url,
                retry_after=retry_after_header(response),
                headers=response.headers,
            )
        except httpx.HTTPStatusError as e:
            # raise_for_status() lives inside the retry scope now, so hard
            # blocks (403/429) surface here — re-check them for BlockEvent
            # before degrading, so quarantine still engages (#55).
            detect_block_event(
                status_code=e.response.status_code,
                body=e.response.text,
                url=url,
                retry_after=retry_after_header(e.response),
                headers=e.response.headers,
            )
            logger.debug("AliExpress fetch failed for %s: %s", url[:80], e)
            return ProductInfo(error=f"HTTP error: {e}", stock_evidence=False)
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("AliExpress fetch failed for %s: %s", url[:80], e)
            return ProductInfo(error=f"HTTP error: {e}", stock_evidence=False)

        info = ProductInfo(stock_evidence=False)
        soup = BeautifulSoup(response.text, "lxml")
        if not page_matches(soup, url, str(response.url)):
            return ProductInfo(error="identity_mismatch", stock_evidence=False)
        structured = product_evidence(soup, url)
        if structured and structured.get("identity_mismatch"):
            return ProductInfo(error="identity_mismatch", stock_evidence=False)
        if structured and structured.get("currency_ambiguous"):
            return ProductInfo(error="currency_ambiguous", stock_evidence=False)
        if structured and structured.get("available") is False:
            return ProductInfo(
                name=structured.get("name"), currency=structured.get("currency"), available=False
            )
        if structured and structured.get("price") is not None and not structured.get("currency"):
            return ProductInfo(error="currency_ambiguous", stock_evidence=False)
        if structured and structured.get("price") is not None:
            return ProductInfo(
                name=structured.get("name"),
                price=structured["price"],
                currency=structured.get("currency"),
                stock_evidence=isinstance(structured.get("available"), bool),
            )

        # Primary: window.runParams JS blob (parsed before soup construction)
        try:
            runparams_result = self._try_runparams(response.text)
            if runparams_result and runparams_result.get("currency_ambiguous"):
                return ProductInfo(error="currency_ambiguous", stock_evidence=False)
            if runparams_result and runparams_result.get("price") is not None:
                info.price = runparams_result["price"]
                if runparams_result.get("currency"):
                    info.currency = runparams_result["currency"]
                if runparams_result.get("name"):
                    info.name = runparams_result["name"]
                logger.debug("aliexpress price via runparams: %s", info.price)
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError, KeyError) as e:
            logger.debug("aliexpress runparams strategy error: %s", e)

        # Fallback strategies (skipped once price+name are populated)
        for strategy_name, strategy_fn in (
            ("jsonld", lambda page: product_evidence(page, url)),
            ("dom", self._try_dom),
        ):
            if info.price is not None and info.name is not None:
                break
            try:
                result = strategy_fn(soup)
                if result and result.get("identity_mismatch"):
                    return ProductInfo(error="identity_mismatch", stock_evidence=False)
                if result and result.get("currency_ambiguous"):
                    return ProductInfo(error="currency_ambiguous", stock_evidence=False)
                if result and result.get("price") is not None and info.price is None:
                    info.price = result["price"]
                    if result.get("currency") and info.currency is None:
                        info.currency = result["currency"]
                    if result.get("name") and info.name is None:
                        info.name = result["name"]
                    if isinstance(result.get("available"), bool):
                        info.available = result["available"]
                        info.stock_evidence = True
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

        if info.price is not None and info.currency is None:
            return ProductInfo(name=info.name, error="currency_ambiguous", stock_evidence=False)

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
        maximum = discounted.get("maxActivityAmount") or discounted.get("maxAmount")
        if not isinstance(maximum, dict) or maximum.get("currency") != amt.get("currency"):
            return None
        upper = parse_price(str(maximum.get("value", "")))
        if upper != price:
            return None
        result = dom_evidence(str(amt.get("value", "")), amt.get("currency"))
        if result is None:
            return None
        # Try to grab name from the same blob (subject)
        subject = data.get("titleModule", {}) if isinstance(data, dict) else {}
        if isinstance(subject, dict):
            name = subject.get("subject")
            if isinstance(name, str) and name:
                result["name"] = name[:200]
        return result

    def _try_dom(self, soup: BeautifulSoup) -> StrategyResult | None:
        value_el = soup.select_one(".product-price-value")
        if not isinstance(value_el, Tag):
            return None
        text = value_el.get_text(strip=True)
        currency_el = (
            value_el.parent.select_one(".product-price-currency")
            if isinstance(value_el.parent, Tag)
            else None
        )
        declared = currency_el.get_text(strip=True) if isinstance(currency_el, Tag) else None
        return dom_evidence(text, declared)
