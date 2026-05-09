"""eBay-specific scraper.

eBay uses structured data (JSON-LD) extensively, making it more reliable
than HTML parsing alone.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, ClassVar

import httpx
from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from decimal import Decimal

from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    get_headers,
    parse_price,
)

logger = logging.getLogger(__name__)


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_ebay_html(url: str, client: httpx.AsyncClient) -> str:
    """Single GET attempt with browser headers. Tenacity handles retries."""
    headers = get_headers()
    response = await client.get(url, headers=headers, follow_redirects=True)
    response.raise_for_status()
    return response.text


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
            return ProductInfo(error="Impossibile caricare la pagina eBay")

        if not html:
            return ProductInfo(error="Impossibile caricare la pagina eBay")

        soup = BeautifulSoup(html, "lxml")
        info = ProductInfo()

        # Strategy 1: JSON-LD (most reliable for eBay)
        info.price = self._try_json_ld(soup, info)

        # Strategy 2: Microdata
        if info.price is None:
            self._try_microdata(soup, info)

        # Strategy 3: CSS selectors
        if info.price is None:
            info.price = self._try_selectors(soup)

        if not info.name:
            info.name = self._extract_name(soup)

        if info.price is None:
            info.error = "Prezzo non trovato su eBay"

        return info

    def _try_json_ld(self, soup: BeautifulSoup, info: ProductInfo) -> Decimal | None:
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("@type", ""))
                    if "Product" in item_type:
                        if not info.name:
                            name = item.get("name", "")
                            if name:
                                info.name = name[:200]

                        offers = item.get("offers", {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        if isinstance(offers, dict):
                            price = offers.get("price") or offers.get("lowPrice")
                            if price is not None:
                                parsed = parse_price(str(price))
                                if parsed:
                                    info.currency = offers.get("priceCurrency", "EUR")
                                    return parsed
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    def _try_microdata(self, soup: BeautifulSoup, info: ProductInfo) -> None:
        price_el = soup.find(attrs={"itemprop": "price"})
        if price_el:
            val = price_el.get("content") or price_el.get_text(strip=True)
            parsed = parse_price(str(val))
            if parsed:
                info.price = parsed

        if not info.name:
            name_el = soup.find(attrs={"itemprop": "name"})
            if name_el:
                name = name_el.get("content") or name_el.get_text(strip=True)
                if name:
                    info.name = name[:200]

    def _try_selectors(self, soup: BeautifulSoup) -> Decimal | None:
        for selector in self.PRICE_SELECTORS:
            try:
                elements = soup.select(selector)
            except (ValueError, AttributeError):
                continue
            for el in elements:
                text = el.get_text(strip=True)
                if text:
                    parsed = parse_price(text)
                    if parsed:
                        return parsed
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
