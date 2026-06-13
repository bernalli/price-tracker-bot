"""Nove25 scraper — parses static HTML metadata.

Strategy priority:
  1. JSON-LD `Product.offers.price` + `priceCurrency`
  2. OpenGraph `product:price:amount` + `product:price:currency`
  3. Microdata `itemprop=price` + `itemprop=priceCurrency`
  4. CSS selector `.product-price` (last resort)

Site is server-rendered: no JavaScript required to read price.
"""

from __future__ import annotations

import json
import logging
import re
from typing import ClassVar

import httpx
from bs4 import BeautifulSoup, Tag

from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    find_microdata_price_el,
    get_headers,
    jsonld_offer_availability,
    parse_price,
)

logger = logging.getLogger(__name__)


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_nove25_html(url: str, client: httpx.AsyncClient) -> str:
    """Single GET with browser headers. Tenacity handles retries."""
    response = await client.get(url, headers=get_headers(), follow_redirects=True)
    response.raise_for_status()
    return response.text


def _tag_attr(tag: Tag, name: str) -> str | None:
    """Return a tag attribute as a stripped string, or None.

    BeautifulSoup typed-stub returns `str | list[str] | None` for `Tag.get`;
    collapse to a clean Optional[str] so callers don't repeat the type-narrowing.
    """
    value = tag.get(name)
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


class Nove25Scraper(AbstractScraper):
    """Scraper for nove25.net — static HTML, no JS."""

    name: ClassVar[str] = "nove25"
    priority: ClassVar[int] = 75
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"(?:^|\.)nove25\.net$", re.IGNORECASE),
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        try:
            html = await _fetch_nove25_html(url, client)
        except httpx.HTTPError as e:
            logger.debug("Nove25 fetch failed for %s: %s", url[:80], e)
            return ProductInfo(error=f"HTTP error: {e}")

        soup = BeautifulSoup(html, "lxml")

        info = (
            self._parse_jsonld(soup)
            or self._parse_opengraph(soup)
            or self._parse_microdata(soup)
            or self._parse_css(soup)
        )
        if info and info.price is not None:
            if info.name is None:
                info.name = self._extract_name(soup)
            if info.currency is None:
                info.currency = "EUR"
            return info

        return ProductInfo(error="Prezzo non trovato (Nove25)")

    @staticmethod
    def _parse_jsonld(soup: BeautifulSoup) -> ProductInfo | None:
        for script in soup.find_all("script", type="application/ld+json"):
            if not isinstance(script, Tag):
                continue
            raw = script.string or script.get_text(strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            payload = Nove25Scraper._coerce_product(data)
            if payload is None:
                continue
            offers = payload.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if not isinstance(offers, dict):
                continue
            raw_price = offers.get("price")
            price = parse_price(str(raw_price)) if raw_price is not None else None
            if price is None:
                continue
            raw_currency = offers.get("priceCurrency")
            currency: str | None = None
            if isinstance(raw_currency, str) and len(raw_currency.strip()) == 3:
                currency = raw_currency.strip().upper()
            raw_name = payload.get("name")
            name = str(raw_name)[:200] if raw_name else None
            available = jsonld_offer_availability(offers)
            return ProductInfo(
                name=name,
                price=price,
                currency=currency,
                available=available if available is not None else True,
            )
        return None

    @staticmethod
    def _coerce_product(data: object) -> dict[str, object] | None:
        """Return the first `@type=Product` dict found in JSON-LD payload."""
        if isinstance(data, list):
            for item in data:
                payload = Nove25Scraper._coerce_product(item)
                if payload is not None:
                    return payload
            return None
        if not isinstance(data, dict):
            return None
        node_type = data.get("@type")
        if node_type == "Product" or (isinstance(node_type, list) and "Product" in node_type):
            return data
        graph = data.get("@graph")
        if isinstance(graph, list):
            return Nove25Scraper._coerce_product(graph)
        return None

    @staticmethod
    def _parse_opengraph(soup: BeautifulSoup) -> ProductInfo | None:
        amount_tag = soup.find("meta", property="product:price:amount") or soup.find(
            "meta", property="og:price:amount"
        )
        if not isinstance(amount_tag, Tag):
            return None
        raw = _tag_attr(amount_tag, "content")
        price = parse_price(raw) if raw else None
        if price is None:
            return None
        currency_tag = soup.find("meta", property="product:price:currency") or soup.find(
            "meta", property="og:price:currency"
        )
        currency: str | None = None
        if isinstance(currency_tag, Tag):
            curr = _tag_attr(currency_tag, "content")
            if curr and len(curr) == 3:
                currency = curr.upper()
        return ProductInfo(price=price, currency=currency)

    @staticmethod
    def _parse_microdata(soup: BeautifulSoup) -> ProductInfo | None:
        price_tag = find_microdata_price_el(soup)
        if not isinstance(price_tag, Tag):
            return None
        raw = _tag_attr(price_tag, "content") or price_tag.get_text(strip=True) or None
        price = parse_price(raw) if raw else None
        if price is None:
            return None
        currency_tag = soup.find(attrs={"itemprop": "priceCurrency"})
        currency: str | None = None
        if isinstance(currency_tag, Tag):
            curr = _tag_attr(currency_tag, "content") or currency_tag.get_text(strip=True)
            if curr and len(curr.strip()) == 3:
                currency = curr.strip().upper()
        return ProductInfo(price=price, currency=currency)

    @staticmethod
    def _parse_css(soup: BeautifulSoup) -> ProductInfo | None:
        el = soup.select_one(".product-price")
        if not isinstance(el, Tag):
            return None
        text = el.get_text(" ", strip=True)
        price = parse_price(text)
        if price is None:
            return None
        return ProductInfo(price=price)

    @staticmethod
    def _extract_name(soup: BeautifulSoup) -> str | None:
        og_title = soup.find("meta", property="og:title")
        if isinstance(og_title, Tag):
            content = _tag_attr(og_title, "content")
            if content:
                return content[:200]
        h1 = soup.find("h1")
        if isinstance(h1, Tag):
            text = h1.get_text(strip=True)
            if text:
                return text[:200]
        return None


__all__ = ["Nove25Scraper"]
