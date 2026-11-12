"""Walmart scraper — extracts product info from walmart.com.

Strategy: JSON-LD Product/offers (primary) with itemprop=price microdata fallback.
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
    find_microdata_price_el,
    get_headers,
)
from price_tracker.scrapers.secondary_evidence import (
    EvidenceResult,
    dom_evidence,
    page_matches,
    product_evidence,
)

logger = logging.getLogger(__name__)


StrategyResult = EvidenceResult


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_walmart_html(url: str, client: httpx.AsyncClient) -> str:
    headers = get_headers()
    page = await fetch_page(url, client, headers=headers)
    return ImagePageHTML(page.text, page.url_final)


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
            html = await _fetch_walmart_html(url, client)
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("Walmart fetch failed for %s: %s", url[:80], e)
            return ProductInfo(stock_evidence=False, error=f"HTTP error: {e}")

        soup = BeautifulSoup(html, "lxml")
        if not page_matches(soup, url, getattr(html, "final_url", url)):
            return ProductInfo(stock_evidence=False, error="identity_mismatch")
        info = ProductInfo(stock_evidence=False)
        unknown_currency = False

        for strategy_name, strategy_fn in (
            ("jsonld", lambda page: self._try_jsonld(page, url)),
            ("microdata", self._try_microdata),
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
                logger.debug("walmart price via %s: %s", strategy_name, info.price)
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

        if info.price is None and unknown_currency:
            return ProductInfo(name=info.name, stock_evidence=False, error="currency_ambiguous")

        if info.price is None:
            info.error = "Price not found in page"

        return info

    def _try_jsonld(self, soup: BeautifulSoup, url: str) -> StrategyResult | None:
        return product_evidence(soup, url)

    def _try_microdata(self, soup: BeautifulSoup) -> StrategyResult | None:
        price_el = find_microdata_price_el(soup)
        if not isinstance(price_el, Tag):
            return None
        raw = price_el.get("content") or price_el.get_text(strip=True)
        # Scope currency/name to the price element's own itemscope (Offer for the
        # currency, enclosing Product for the name): a page-wide find would pick
        # up a related-items carousel's values (#37). Flat markup without any
        # itemscope keeps the page-wide lookup.
        offer_scope = price_el.find_parent(attrs={"itemscope": True})
        currency_root = offer_scope if isinstance(offer_scope, Tag) else price_el.parent
        name_root: Tag | BeautifulSoup = soup
        if isinstance(offer_scope, Tag):
            product_scope = offer_scope.find_parent(attrs={"itemscope": True})
            name_root = product_scope if isinstance(product_scope, Tag) else offer_scope
        currency_el = (
            currency_root.find(attrs={"itemprop": "priceCurrency"}, recursive=False)
            if isinstance(currency_root, Tag)
            else None
        )
        declared = (
            (currency_el.get("content") or currency_el.get_text(strip=True))
            if isinstance(currency_el, Tag)
            else None
        )
        result = dom_evidence(str(raw), declared)
        if result is None:
            return None
        name_el = name_root.find(attrs={"itemprop": "name"})
        if isinstance(name_el, Tag):
            name_val = name_el.get("content") or name_el.get_text(strip=True)
            if name_val:
                result["name"] = str(name_val)[:200]
        return result
