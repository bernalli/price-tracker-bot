"""eBay-specific scraper.

eBay uses structured data (JSON-LD) extensively, making it more reliable
than HTML parsing alone.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, ClassVar

import httpx
from bs4 import BeautifulSoup

from price_tracker.core.fetch import fetch_page
from price_tracker.core.page_image import ImagePageHTML

if TYPE_CHECKING:
    from decimal import Decimal

from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    find_microdata_price_el,
    get_headers,
)
from price_tracker.scrapers.secondary_evidence import (
    dom_evidence,
    page_matches,
    product_evidence,
)

logger = logging.getLogger(__name__)


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_ebay_html(url: str, client: httpx.AsyncClient) -> str:
    """Single GET attempt with browser headers. Tenacity handles retries."""
    headers = get_headers()
    page = await fetch_page(url, client, headers=headers)
    return ImagePageHTML(page.text, page.url_final)


class EbayScraper(AbstractScraper):
    """Scraper for eBay listings across major locales."""

    name: ClassVar[str] = "ebay"
    priority: ClassVar[int] = 90
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"^(www\.)?ebay\.(com|it|de|co\.uk|fr|es|nl|pl|com\.au|ca)$"),
    ]

    PRICE_SELECTORS: ClassVar[list[str]] = [
        # Modern eBay layout
        ".x-price-primary .ux-textspans",
        ".x-price-primary span[itemprop='price']",
        ".x-bin-price .ux-textspans",
        # Older layout
        "#prcIsum",
        "#prcIsum_bidPrice",
        "#mm-saleDscPrc",
        ".display-price",
        # Buy It Now
        ".notranslate",
        "#vi-cPrice-lbl + span",
    ]

    NAME_SELECTORS: ClassVar[list[str]] = [
        ".x-item-title__mainTitle span",
        "h1.x-item-title__mainTitle",
        "#itemTitle",
        "h1[itemprop='name']",
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        try:
            html = await _fetch_ebay_html(url, client)
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("eBay fetch failed for %s: %s", url[:60], e)
            return ProductInfo(stock_evidence=False, error="Impossibile caricare la pagina eBay")

        if not html:
            return ProductInfo(stock_evidence=False, error="Impossibile caricare la pagina eBay")

        soup = BeautifulSoup(html, "lxml")
        if not page_matches(soup, url, getattr(html, "final_url", url)):
            return ProductInfo(stock_evidence=False, error="identity_mismatch")
        info = ProductInfo(stock_evidence=False)

        # Strategy 1: JSON-LD (most reliable for eBay)
        info.price = self._try_json_ld(soup, info, url)
        if info.available is False or info.error in {"currency_ambiguous", "identity_mismatch"}:
            return info

        # Strategy 2: Microdata
        if info.price is None:
            self._try_microdata(soup, info)
        if info.error:
            return info

        # Strategy 3: CSS selectors
        if info.price is None:
            info.price = self._try_selectors(soup, info)

        if not info.name:
            info.name = self._extract_name(soup)

        if info.price is not None and info.currency is None:
            return ProductInfo(name=info.name, stock_evidence=False, error="currency_ambiguous")
        if info.price is None and info.error is None:
            info.error = "Prezzo non trovato su eBay"

        return info

    def _try_json_ld(self, soup: BeautifulSoup, info: ProductInfo, url: str) -> Decimal | None:
        result = product_evidence(soup, url)
        if result is None:
            return None
        info.name = result.get("name")
        info.currency = result.get("currency")
        if result.get("identity_mismatch"):
            info.error = "identity_mismatch"
        elif result.get("currency_ambiguous"):
            info.error = "currency_ambiguous"
        elif result.get("available") is not None:
            info.available = result["available"]
            info.stock_evidence = True
        return result.get("price")

    def _try_microdata(self, soup: BeautifulSoup, info: ProductInfo) -> None:
        # Prefer the main listing container: related/recommended carousels often
        # expose their own itemprop=price (even a full offers scope) earlier in
        # document order and would win a page-wide microdata lookup (#31).
        price_el = None
        main = soup.select_one(".x-price-primary")
        if main is not None:
            price_el = main.find(attrs={"itemprop": "price"})
        if price_el is None:
            price_el = find_microdata_price_el(soup)
        if price_el:
            val = price_el.get("content") or price_el.get_text(strip=True)
            scope = price_el.find_parent(attrs={"itemscope": True}) or main or price_el.parent
            currency_el = scope.find(attrs={"itemprop": "priceCurrency"}) if scope else None
            declared = (
                (currency_el.get("content") or currency_el.get_text(strip=True))
                if currency_el
                else None
            )
            context_text = (main or price_el).get_text(" ", strip=True)
            visible = dom_evidence(context_text, declared)
            if context_text and visible is None:
                # A numeric content attribute cannot override an explicitly
                # non-purchase visible context (installments, ranges, etc.).
                info.error = "Price is not a single purchase amount"
                return
            if visible and visible.get("currency_ambiguous"):
                info.error = "currency_ambiguous"
                return
            if declared is None and visible:
                declared = visible.get("currency")
            result = dom_evidence(str(val), declared)
            if result:
                info.price = result.get("price")
                info.currency = result.get("currency")
                if result.get("currency_ambiguous"):
                    info.error = "currency_ambiguous"

        if not info.name:
            name_el = soup.find(attrs={"itemprop": "name"})
            if name_el:
                name = name_el.get("content") or name_el.get_text(strip=True)
                if name:
                    info.name = name[:200]

    def _try_selectors(self, soup: BeautifulSoup, info: ProductInfo) -> Decimal | None:
        for selector in self.PRICE_SELECTORS:
            try:
                elements = soup.select(selector)
            except (ValueError, AttributeError):
                continue
            for el in elements:
                scope = el.find_parent(class_=["x-price-primary", "x-bin-price"]) or el
                text = scope.get_text(" ", strip=True)
                if text:
                    result = dom_evidence(text)
                    if result:
                        info.currency = result.get("currency")
                        if result.get("currency_ambiguous"):
                            info.error = "currency_ambiguous"
                        return result.get("price")
        return None

    def _extract_name(self, soup: BeautifulSoup) -> str | None:
        for selector in self.NAME_SELECTORS:
            el = soup.select_one(selector)
            if el:
                name = el.get_text(strip=True)
                # eBay sometimes prepends "Details about  " to item title
                name = re.sub(r"^Details about\s+", "", name, flags=re.IGNORECASE)
                name = re.sub(r"^Dettagli su\s+", "", name, flags=re.IGNORECASE)
                if name:
                    return name[:200]
        return None
