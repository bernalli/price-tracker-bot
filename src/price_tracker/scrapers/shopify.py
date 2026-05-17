"""Shopify scraper — uses the public /products/{handle}.json API.

Works on any Shopify-powered store without needing JS rendering.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import ClassVar
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    detect_currency,
    get_headers,
    parse_price,
)

logger = logging.getLogger(__name__)


_SHOPIFY_PRODUCT_PATH_RE = re.compile(r"(?:^|/)products/[a-z0-9\-_]+", re.IGNORECASE)


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_shopify_response(url: str, client: httpx.AsyncClient) -> httpx.Response:
    """Single GET attempt with browser headers. Tenacity handles retries."""
    headers = get_headers()
    response = await client.get(url, headers=headers, follow_redirects=True)
    response.raise_for_status()
    return response


def _is_product_path(url: httpx.URL | str) -> bool:
    """True when the URL path contains a Shopify-style /products/<slug> segment.

    Used to reject home/collection redirects that would otherwise let the HTML
    fallback parse a random price out of an unrelated page.
    """
    path = url.path if isinstance(url, httpx.URL) else urlparse(str(url)).path
    return bool(_SHOPIFY_PRODUCT_PATH_RE.search(path))


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_shopify_json(json_url: str, client: httpx.AsyncClient) -> httpx.Response:
    """Single GET attempt against the /products/{handle}.json endpoint."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }
    response = await client.get(json_url, headers=headers, follow_redirects=True)
    # Don't raise_for_status here — caller handles 403 specially.
    return response


class ShopifyScraper(AbstractScraper):
    """Scraper for Shopify-powered stores using the public JSON API."""

    name: ClassVar[str] = "shopify"
    priority: ClassVar[int] = 80
    # Shopify uses content/path-based detection, not domain regex
    domain_patterns: ClassVar[list[re.Pattern[str]]] = []

    # Known Shopify domains (extend as discovered)
    KNOWN_SHOPIFY_DOMAINS: ClassVar[set[str]] = {
        "fillingpieces.com",
        "allbirds.com",
        "gymshark.com",
        "colourpop.com",
        "fashionnova.com",
        "kith.com",
        "bombas.com",
        "brooklinen.com",
        "decodedgear.com",
    }

    def can_handle(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except (ValueError, TypeError):
            return False
        domain = parsed.netloc.replace("www.", "")

        # Known Shopify domains
        if domain in self.KNOWN_SHOPIFY_DOMAINS:
            return True

        # URL pattern: /products/{handle} (common Shopify pattern)
        return bool(re.search(r"/products/[a-z0-9\-]+", parsed.path))

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        # Try JSON API first (most reliable)
        json_url = self._build_json_url(url)
        if json_url:
            result = await self._try_json_api(json_url, client)
            if result and result.price is not None:
                # Detect currency from HTML page (JSON-LD, OG tags)
                html = await self._fetch_html(url, client)
                if html:
                    detected = self._detect_currency_from_html(html)
                    if detected:
                        result.currency = detected
                if not result.currency:
                    result.currency = detect_currency("")
                return result

        # Fallback: fetch HTML and parse embedded Shopify product data
        html = await self._fetch_html(url, client)
        if html:
            result = self._try_html_extraction(html)
            if result and result.price is not None:
                result.currency = detect_currency("")
                return result

            soup = BeautifulSoup(html, "lxml")
            css_result = self._try_shopify_selectors(soup)
            if css_result:
                return css_result

        return ProductInfo(error="Prezzo non trovato (Shopify)")

    @staticmethod
    async def _fetch_html(url: str, client: httpx.AsyncClient) -> str | None:
        try:
            response = await _fetch_shopify_response(url, client)
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("Shopify HTML fetch failed for %s: %s", url[:60], e)
            return None
        if not _is_product_path(response.url):
            logger.info(
                "Shopify rejecting non-product redirect: %s -> %s",
                url[:80],
                str(response.url)[:80],
            )
            return None
        return response.text

    def _build_json_url(self, url: str) -> str | None:
        """Convert product URL to JSON API endpoint."""
        try:
            parsed = urlparse(url)
        except (ValueError, TypeError):
            return None

        match = re.search(r"(/products/[a-z0-9\-_]+)", parsed.path)
        if match:
            product_path = match.group(1)
            # Preserve locale prefix (e.g. /en-it/products/xxx → /en-it/products/xxx.json)
            locale_match = re.match(
                r"(/[a-z]{2}(?:-[a-z]{2})?)?(/products/)",
                parsed.path,
                re.IGNORECASE,
            )
            if locale_match and locale_match.group(1):
                product_path = locale_match.group(1) + product_path
            return f"{parsed.scheme}://{parsed.netloc}{product_path}.json"
        return None

    async def _try_json_api(self, json_url: str, client: httpx.AsyncClient) -> ProductInfo | None:
        """Fetch product data from Shopify JSON API."""
        try:
            logger.debug("Shopify JSON API: fetching %s", json_url)
            response = await _fetch_shopify_json(json_url, client)
            logger.debug("Shopify JSON API: status %s", response.status_code)

            if response.status_code == 403:
                # Try curl_cffi fallback
                data = await self._fetch_json_via_curl_cffi(json_url)
                if data is None:
                    return None
            elif response.status_code != 200:
                return None
            else:
                data = response.json()

            product = data.get("product", {})
            if not product:
                logger.warning("Shopify JSON API: no 'product' key in response")
                return None

            name = product.get("title")

            variants = product.get("variants", [])
            logger.debug("Shopify JSON API: %s, %d variants", name, len(variants))
            price: Decimal | None = None
            for variant in variants:
                variant_price = variant.get("price")
                if variant_price:
                    parsed = parse_price(str(variant_price))
                    logger.debug("Shopify variant price: %s -> %s", variant_price, parsed)
                    if parsed:
                        price = parsed
                        break

            if price is None:
                return None

            return ProductInfo(
                name=name,
                price=price,
                currency=None,  # Unknown from JSON API; detected from HTML downstream
            )

        except (json.JSONDecodeError, httpx.HTTPError, ValueError, KeyError, AttributeError) as e:
            logger.debug("Shopify JSON API error for %s: %s", json_url, e)
            return None

    @staticmethod
    async def _fetch_json_via_curl_cffi(json_url: str) -> dict | None:
        """Fallback for 403 from Shopify JSON endpoint."""
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            return None
        try:
            async with AsyncSession(impersonate="chrome") as session:
                resp = await session.get(json_url, allow_redirects=True, timeout=30)
                if resp.status_code == 200:
                    return resp.json()
        except (ValueError, OSError, AttributeError) as e:
            logger.debug("Shopify curl_cffi fallback failed: %s", e)
        return None

    def _try_html_extraction(self, html: str) -> ProductInfo | None:
        """Extract product data from embedded Shopify JSON in HTML."""
        info = ProductInfo()

        patterns = [
            # var meta = {"product": {...}}
            r"var\s+meta\s*=\s*(\{.+?\});",
            # <script.*id="ProductJson.*">
            r'<script[^>]*id=["\']ProductJson[^"\']*["\'][^>]*>(\{.+?\})</script>',
            # window.ShopifyAnalytics.meta
            r"ShopifyAnalytics\.meta\s*=\s*(\{.+?\});",
            # product: { ... } in script tags
            r'"product"\s*:\s*(\{[^}]*"variants"[^}]*\})',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, html, re.DOTALL)
            for match in matches:
                try:
                    data = json.loads(match)
                    product = data.get("product", data)
                    if "variants" in product:
                        info.name = product.get("title")
                        for v in product["variants"]:
                            price = v.get("price")
                            if price:
                                info.price = parse_price(str(price))
                                if info.price:
                                    return info
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue

        # Also try Shopify-style price in cents (e.g. 2999 = €29.99)
        cents_patterns = [
            r'"price"\s*:\s*(\d{3,7})\b',
            r'"price_min"\s*:\s*(\d{3,7})\b',
        ]
        for pattern in cents_patterns:
            matches = re.findall(pattern, html)
            for match in matches:
                try:
                    cents = int(match)
                    if 100 <= cents <= 10000000:
                        info.price = Decimal(cents) / Decimal(100)
                        return info
                except (ValueError, TypeError):
                    continue

        return None

    def _detect_currency_from_html(self, html: str) -> str | None:
        """Detect currency from HTML page using JSON-LD, OG tags, and Shopify JS."""
        soup = BeautifulSoup(html, "lxml")

        # 1. OG tag: og:price:currency
        og_currency = soup.find("meta", property="og:price:currency")
        if og_currency:
            curr = og_currency.get("content", "").strip().upper()
            if len(curr) == 3:
                return curr

        # 2. product:price:currency
        prod_currency = soup.find("meta", property="product:price:currency")
        if prod_currency:
            curr = prod_currency.get("content", "").strip().upper()
            if len(curr) == 3:
                return curr

        # 3. JSON-LD priceCurrency
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or script.get_text(strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            data = item
                            break
                if isinstance(data, dict):
                    offers = data.get("offers", {})
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    if isinstance(offers, dict):
                        curr = offers.get("priceCurrency", "").strip().upper()
                        if len(curr) == 3:
                            return curr
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

        # 4. Shopify JS: Shopify.currency.active
        m = re.search(r'"currency"\s*:\s*"([A-Z]{3})"', html)
        if m:
            return m.group(1)

        return None

    @staticmethod
    def _try_shopify_selectors(soup: BeautifulSoup) -> ProductInfo | None:
        """Try Shopify-specific CSS selectors."""
        info = ProductInfo()

        selectors = [
            ".product-single__price",
            ".product__price",
            ".price__current",
            ".price-item--sale",
            ".price-item--regular",
            "[data-product-price]",
            ".product-price",
            ".price .money",
            ".ProductMeta__Price",
            ".product-single__meta .price",
        ]

        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                val = el.get("data-product-price") or el.get("content") or el.get_text(strip=True)
                if val:
                    price = parse_price(str(val))
                    if price:
                        info.price = price
                        break

        for sel in [
            "h1.product-single__title",
            "h1.ProductMeta__Title",
            ".product__title h1",
            "h1",
        ]:
            el = soup.select_one(sel)
            if el:
                info.name = el.get_text(strip=True)[:200]
                break

        return info if info.price else None
