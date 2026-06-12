"""Generic scraper — extracts product info using standard web markup.

Works on any site using JSON-LD, microdata, Open Graph, RDFa, or common
HTML patterns. Extraction priority:

  1. JSON-LD structured data (schema.org Product)
  2. Microdata attributes (itemprop="price")
  3. Open Graph meta tags (og:price:amount)
  4. RDFa attributes (typeof="Product"/"Offer", property="price")
  5. Additional meta tags (product:price:amount, twitter:data1)
  6. Common CSS selectors for e-commerce sites
  7. data-* attributes containing price info
  8. Inline JS product data
  9. Regex fallback on visible text (heuristic; logs a warning)
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import ClassVar

import httpx
from bs4 import BeautifulSoup, Tag

from price_tracker.core.exceptions import BlockEvent
from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_currency,
    get_headers,
    parse_price,
)

logger = logging.getLogger(__name__)


# Product types recognised in JSON-LD
PRODUCT_TYPES: frozenset[str] = frozenset(
    {
        "Product",
        "IndividualProduct",
        "ProductModel",
        "SomeProducts",
        "ProductGroup",
        "Vehicle",
        "Car",
    }
)


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_generic_html(url: str, client: httpx.AsyncClient) -> str:
    """Single GET attempt with browser headers. Tenacity handles retries."""
    headers = get_headers()
    response = await client.get(url, headers=headers, follow_redirects=True)
    # Surface 403/429 (and WAF/CAPTCHA bodies) as a BlockEvent BEFORE
    # raise_for_status, so the scheduler quarantines the domain instead of
    # recording a generic failure (#16). with_retry never retries BlockEvents.
    detect_block_event(status_code=response.status_code, body=response.text, url=url)
    response.raise_for_status()
    return response.text


async def _fetch_with_curl_cffi(url: str) -> str | None:
    """Fetch via curl_cffi with Chrome JA3/TLS impersonation. Returns None on failure."""
    try:
        from curl_cffi import CurlError
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return None
    try:
        async with AsyncSession(impersonate="chrome") as session:
            resp = await session.get(url, allow_redirects=True, timeout=30)
            if resp.status_code in (403, 429):
                # Hard block on the primary path: raise instead of discarding the
                # status, so scrape() can quarantine after fallbacks fail (#16).
                detect_block_event(status_code=resp.status_code, body=resp.text or "", url=url)
            if 200 <= resp.status_code < 300 and resp.text:
                return resp.text
            logger.debug("curl_cffi got %s for %s", resp.status_code, url[:60])
    except (CurlError, ValueError, OSError, AttributeError) as e:
        logger.debug("curl_cffi fetch failed for %s: %s", url[:60], e)
    return None


class GenericScraper(AbstractScraper):
    """Catch-all scraper running multi-strategy extraction."""

    name: ClassVar[str] = "generic"
    priority: ClassVar[int] = 0
    domain_patterns: ClassVar[list[re.Pattern[str]]] = []

    # Extended list of selectors found across many e-commerce platforms
    PRICE_SELECTORS: ClassVar[list[str]] = [
        # Generic e-commerce
        "[data-price]",
        "[data-product-price]",
        "[data-current-price]",
        "[data-sale-price]",
        # Common class patterns
        ".product-price",
        ".product-price-value",
        ".productPrice",
        ".price-current",
        ".current-price",
        ".sale-price",
        ".final-price",
        ".special-price .price",
        ".offer-price",
        ".price--selling",
        ".price--current",
        ".price-value",
        ".price_value",
        ".priceValue",
        "#product-price",
        "#productPrice",
        # Price box patterns
        ".price-box .price",
        ".price-box .special-price",
        ".product__price",
        ".product-info-price .price",
        ".product-detail-price .price",
        ".price .amount",
        ".woocommerce-Price-amount",
        ".shopify-price",
        # Italian e-commerce specific
        ".prezzo-attuale",
        ".prezzo-vendita",
        ".prezzo_vendita",
        ".prezzo",
        ".prezzoFinale",
        ".price-main .price",
        # WooCommerce
        ".woocommerce-Price-amount.amount",
        "p.price ins .woocommerce-Price-amount",
        "p.price > .woocommerce-Price-amount",
        ".summary .price .woocommerce-Price-amount",
        # PrestaShop
        ".product-price .current-price-value",
        ".product-prices .current-price-value",
        "[itemprop='price']",
        # Shopify
        ".product-single__price",
        ".product__price",
        "[data-product-price]",
        ".price__current",
        ".price-item--sale",
        ".price-item--regular",
    ]

    PRICE_PATTERNS: ClassVar[list[str]] = [
        # €29,99 or €29.99 or € 1.299,99
        r"€\s*(\d{1,3}(?:[.,]\d{3})*[.,]\d{1,2})",
        r"(\d{1,3}(?:[.,]\d{3})*[.,]\d{1,2})\s*€",
        # EUR 29,99
        r"EUR\s*(\d{1,3}(?:[.,]\d{3})*[.,]\d{1,2})",
        r"(\d{1,3}(?:[.,]\d{3})*[.,]\d{1,2})\s*EUR",
        # prezzo/price labels followed by amount
        r"(?:prezzo|price|costo|costa)[:\s]*€?\s*(\d{1,3}(?:[.,]\d{3})*[.,]\d{1,2})",
    ]

    def can_handle(self, url: str) -> bool:  # noqa: ARG002
        return True  # Fallback — handles everything

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        # Anti-bot primary: try curl_cffi (Chrome JA3) first — covers many
        # Shopify / boutique e-commerce fronts that drop plain httpx requests.
        html: str | None = None
        curl_block: BlockEvent | None = None
        try:
            html = await _fetch_with_curl_cffi(url)
        except BlockEvent as e:
            # Defer: give httpx a chance before signalling quarantine (#16).
            curl_block = e
        if not html or len(html) < 5000:
            try:
                fetched = await _fetch_generic_html(url, client)
                if fetched:
                    html = fetched
            except (httpx.HTTPError, ValueError) as e:
                logger.debug("Generic httpx fetch failed for %s: %s", url[:60], e)
                # Keep curl_cffi result (if any)
        if not html:
            if curl_block is not None:
                # Both paths failed and curl_cffi saw a hard block (403/429):
                # surface it so the scheduler quarantines the domain (#16).
                raise curl_block
            return ProductInfo(error="Impossibile caricare la pagina")

        # Surface WAF/CAPTCHA challenge bodies (often served with HTTP 200) as a
        # BlockEvent so the scheduler quarantines the domain (#7).
        detect_block_event(status_code=200, body=html, url=url)

        soup = BeautifulSoup(html, "lxml")
        info = ProductInfo()

        strategies = [
            ("JSON-LD", self._try_json_ld),
            ("Microdata", self._try_microdata),
            ("OpenGraph", self._try_opengraph),
            ("RDFa", self._try_rdfa),
            ("Meta tags", self._try_meta_tags),
            ("CSS selectors", self._try_css_selectors),
            ("Data attributes", self._try_data_attributes),
            ("JS product data", self._try_js_product_data),
            ("Regex", self._try_regex),
        ]

        successful_strategy: str | None = None
        for strategy_name, strategy_fn in strategies:
            try:
                result = strategy_fn(soup)
                if result:
                    if result.get("price") is not None and info.price is None:
                        info.price = result["price"]
                        successful_strategy = strategy_name
                        logger.debug("Price found via %s: %s", strategy_name, info.price)
                    if result.get("name") and info.name is None:
                        info.name = result["name"]
                    if result.get("currency") and info.currency is None:
                        info.currency = result["currency"]

                if info.price is not None and info.name is not None:
                    break
            except (ValueError, KeyError, AttributeError) as e:
                logger.debug("Strategy %s error: %s", strategy_name, e)
                continue

        if successful_strategy == "Regex":
            # Heuristic regex on visible text is the last-resort path. Log a
            # warning so operators can spot domains that need a tuned scraper.
            logger.warning(
                "generic.heuristic_fallback: price extracted via regex heuristic for %s",
                url,
            )

        # Fallback name from <title>
        if not info.name:
            title_tag = soup.find("title")
            if title_tag and title_tag.string:
                title = title_tag.string.strip()
                for sep in [" | ", " - ", " — ", " – ", " :: ", " >> "]:
                    if sep in title:
                        title = title.split(sep)[0].strip()
                info.name = title[:200]

        if info.currency is None:
            info.currency = detect_currency(str(info.price or ""))

        if info.price is None:
            info.error = "Prezzo non trovato nella pagina"

        return info

    # ── Strategy 1: JSON-LD ────────────────────────────────────────

    def _try_json_ld(self, soup: BeautifulSoup) -> dict | None:
        # Method 1: BeautifulSoup extraction
        scripts = soup.find_all("script", type="application/ld+json")
        logger.debug("JSON-LD: found %d script tags via BS4", len(scripts))
        for i, script in enumerate(scripts):
            raw = script.string
            if not raw:
                raw = script.get_text(strip=True)
            if not raw:
                logger.debug("JSON-LD #%d: empty content via BS4", i)
                continue
            result = self._parse_jsonld_raw(raw, f"BS4#{i}")
            if result:
                return result

        # Method 2: Regex fallback directly on HTML
        html_str = str(soup)
        for i, m in enumerate(
            re.finditer(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html_str,
                re.DOTALL,
            )
        ):
            raw = m.group(1).strip()
            if not raw:
                continue
            result = self._parse_jsonld_raw(raw, f"Regex#{i}")
            if result:
                return result

        return None

    def _parse_jsonld_raw(self, raw: str, source: str) -> dict | None:
        try:
            raw = raw.strip()
            if raw.startswith("//"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            data = json.loads(raw)
            # Handle JSON-LD arrays
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") in PRODUCT_TYPES:
                        data = item
                        break
                else:
                    return None
            schema_type = data.get("@type", "unknown") if isinstance(data, dict) else "list"
            logger.debug("JSON-LD %s: type=%s", source, schema_type)
            result = self._extract_from_schema(data)
            if result and result.get("price") is not None:
                logger.info("JSON-LD %s: extracted price %s", source, result.get("price"))
                return result
            if result:
                logger.debug("JSON-LD %s: found data but no price: %s", source, list(result.keys()))
        except (json.JSONDecodeError, ValueError, KeyError, AttributeError) as e:
            logger.warning("JSON-LD %s parse error: %s: %s", source, type(e).__name__, e)
        return None

    def _extract_from_schema(self, data: object, depth: int = 0) -> dict | None:
        if depth > 8:
            return None

        if isinstance(data, list):
            for item in data:
                result = self._extract_from_schema(item, depth + 1)
                if result:
                    return result
            return None

        if not isinstance(data, dict):
            return None

        schema_type_raw = data.get("@type", "")
        schema_type = (
            " ".join(str(t) for t in schema_type_raw)
            if isinstance(schema_type_raw, list)
            else str(schema_type_raw)
        )

        is_product = any(
            t.lower() in schema_type.lower()
            for t in ("Product", "IndividualProduct", "ProductModel", "SomeProducts")
        )

        if is_product:
            result: dict = {}
            result["name"] = data.get("name")

            offers = data.get("offers", data.get("Offers", {}))
            price_info = self._extract_price_from_offers(offers)
            if price_info:
                result.update(price_info)
                return result

            # Direct price on product
            for price_key in ("price", "lowPrice", "highPrice"):
                price = data.get(price_key)
                if price is not None:
                    parsed = parse_price(str(price))
                    if parsed:
                        result["price"] = parsed
                        result["currency"] = data.get("priceCurrency", "EUR")
                        return result

        # Recurse into @graph
        if "@graph" in data:
            result = self._extract_from_schema(data["@graph"], depth + 1)
            if result:
                return result

        # Recurse into mainEntity
        if "mainEntity" in data:
            result = self._extract_from_schema(data["mainEntity"], depth + 1)
            if result:
                return result

        # Recurse into nested dicts that might contain Products
        if not is_product and depth < 3:
            for key, value in data.items():
                if key.startswith("@"):
                    continue
                if isinstance(value, (dict, list)):
                    result = self._extract_from_schema(value, depth + 1)
                    if result:
                        return result

        return None

    def _extract_price_from_offers(self, offers: object) -> dict | None:
        if isinstance(offers, list):
            best: dict | None = None
            for offer in offers:
                result = self._extract_price_from_offers(offer)
                if result and (best is None or result["price"] < best["price"]):
                    best = result
            return best

        if not isinstance(offers, dict):
            return None

        offer_type = str(offers.get("@type", ""))
        if "AggregateOffer" in offer_type:
            low_price = offers.get("lowPrice")
            if low_price is not None:
                parsed = parse_price(str(low_price))
                if parsed:
                    return {
                        "price": parsed,
                        "currency": offers.get("priceCurrency", "EUR"),
                    }

        for price_key in ("price", "lowPrice"):
            price = offers.get(price_key)
            if price is not None:
                parsed = parse_price(str(price))
                if parsed:
                    return {
                        "price": parsed,
                        "currency": offers.get("priceCurrency", "EUR"),
                    }

        if "offers" in offers:
            return self._extract_price_from_offers(offers["offers"])

        return None

    # ── Strategy 2: Microdata ──────────────────────────────────────

    def _try_microdata(self, soup: BeautifulSoup) -> dict | None:
        result: dict = {}

        for price_el in soup.find_all(attrs={"itemprop": "price"}):
            price_val = price_el.get("content")
            if not price_val:
                price_val = price_el.get("value")
            if not price_val:
                price_val = price_el.get_text(strip=True)
            if price_val:
                parsed = parse_price(str(price_val))
                if parsed:
                    result["price"] = parsed
                    break

        currency_el = soup.find(attrs={"itemprop": "priceCurrency"})
        if currency_el:
            result["currency"] = currency_el.get("content", "EUR")

        name_el = soup.find(attrs={"itemprop": "name"})
        if name_el:
            name = name_el.get("content") or name_el.get_text(strip=True)
            if name:
                result["name"] = name[:200]

        return result if result.get("price") else None

    # ── Strategy 3: Open Graph ─────────────────────────────────────

    def _try_opengraph(self, soup: BeautifulSoup) -> dict | None:
        result: dict = {}

        og_price = soup.find("meta", property="og:price:amount") or soup.find(
            "meta", attrs={"name": "og:price:amount"}
        )
        if og_price:
            parsed = parse_price(og_price.get("content", ""))
            if parsed:
                result["price"] = parsed

        og_currency = soup.find("meta", property="og:price:currency")
        if og_currency:
            result["currency"] = og_currency.get("content", "EUR")

        og_title = soup.find("meta", property="og:title")
        if og_title:
            content = og_title.get("content", "")
            if content:
                result["name"] = content[:200]

        return result if result.get("price") else None

    # ── Strategy 4: RDFa ───────────────────────────────────────────

    def _try_rdfa(self, soup: BeautifulSoup) -> dict | None:
        product = soup.find(attrs={"typeof": re.compile(r"\bProduct\b")})
        if not isinstance(product, Tag):
            return None
        offer_match = product.find(attrs={"typeof": re.compile(r"\bOffer\b")})
        offer: Tag = offer_match if isinstance(offer_match, Tag) else product
        price_tag = offer.find(attrs={"property": "price"})
        if not isinstance(price_tag, Tag):
            return None
        price_str = price_tag.get("content") or price_tag.get_text(strip=True)
        parsed = parse_price(str(price_str))
        if parsed is None or parsed <= 0:
            return None
        result: dict = {"price": parsed}
        currency_tag = offer.find(attrs={"property": "priceCurrency"})
        currency_val = currency_tag.get("content") if isinstance(currency_tag, Tag) else None
        if currency_val:
            result["currency"] = currency_val
        name_tag = product.find(attrs={"property": "name"})
        if isinstance(name_tag, Tag):
            name = name_tag.get_text(strip=True)
            if name:
                result["name"] = name[:200]
        return result

    # ── Strategy 5: Additional meta tags ───────────────────────────

    def _try_meta_tags(self, soup: BeautifulSoup) -> dict | None:
        result: dict = {}

        for prop in [
            "product:price:amount",
            "product:price",
            "twitter:data1",
        ]:
            el = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            if el:
                content = el.get("content", "")
                parsed = parse_price(content)
                if parsed:
                    result["price"] = parsed
                    break

        for prop in ["product:price:currency", "product:currency"]:
            el = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            if el:
                result["currency"] = el.get("content", "EUR")
                break

        for name_attr in ["price", "Price", "amount", "product-price"]:
            el = soup.find("meta", attrs={"name": name_attr})
            if el and not result.get("price"):
                parsed = parse_price(el.get("content", ""))
                if parsed:
                    result["price"] = parsed

        return result if result.get("price") else None

    # ── Strategy 6: CSS selectors ──────────────────────────────────

    def _try_css_selectors(self, soup: BeautifulSoup) -> dict | None:
        for selector in self.PRICE_SELECTORS:
            try:
                elements = soup.select(selector)
            except (ValueError, AttributeError):
                continue
            for el in elements:
                for attr in (
                    "data-price",
                    "data-product-price",
                    "data-current-price",
                    "data-sale-price",
                    "content",
                    "value",
                ):
                    data_val = el.get(attr)
                    if data_val:
                        parsed = parse_price(str(data_val))
                        if parsed:
                            return {"price": parsed}

                text = el.get_text(strip=True)
                if text:
                    parsed = parse_price(text)
                    if parsed:
                        return {"price": parsed}
        return None

    # ── Strategy 7: Data attributes ────────────────────────────────

    def _try_data_attributes(self, soup: BeautifulSoup) -> dict | None:
        price_data_attrs = [
            "data-price",
            "data-product-price",
            "data-amount",
            "data-current-price",
            "data-sale-price",
            "data-final-price",
            "data-raw-price",
            "data-price-amount",
            "data-value",
        ]

        for attr in price_data_attrs:
            elements = soup.find_all(attrs={attr: True})
            for el in elements:
                val = el.get(attr, "")
                parsed = parse_price(str(val))
                if parsed:
                    return {"price": parsed}

        return None

    # ── Strategy 8: JS embedded product data ───────────────────────

    def _try_js_product_data(self, soup: BeautifulSoup) -> dict | None:
        scripts = soup.find_all("script")
        for script in scripts:
            if not script.string:
                continue
            text = script.string

            if len(text) > 200000:
                continue

            for pattern in [
                r'"price"\s*:\s*"?(\d{1,6}(?:\.\d{1,2})?)"?',
                r'"salePrice"\s*:\s*"?(\d{1,6}(?:\.\d{1,2})?)"?',
                r'"sale_price"\s*:\s*"?(\d{1,6}(?:\.\d{1,2})?)"?',
                r'"current_price"\s*:\s*"?(\d{1,6}(?:\.\d{1,2})?)"?',
                r'"finalPrice"\s*:\s*"?(\d{1,6}(?:\.\d{1,2})?)"?',
                r'"selling_price"\s*:\s*"?(\d{1,6}(?:\.\d{1,2})?)"?',
            ]:
                matches = re.findall(pattern, text)
                for match in matches:
                    parsed = parse_price(match)
                    if parsed and Decimal("0.50") < parsed < Decimal("100000"):
                        return {"price": parsed}

            if "variants" in text and "price" in text:
                for json_match in re.finditer(r'\{[^{}]*"price"\s*:\s*"?\d+\.?\d*"?[^{}]*\}', text):
                    try:
                        obj = json.loads(json_match.group())
                        price = obj.get("price")
                        if price:
                            parsed = parse_price(str(price))
                            if parsed:
                                return {
                                    "price": parsed,
                                    "name": obj.get("name") or obj.get("title"),
                                }
                    except (json.JSONDecodeError, AttributeError):
                        pass

        return None

    # ── Strategy 9: Regex fallback ─────────────────────────────────

    def _try_regex(self, soup: BeautifulSoup) -> dict | None:
        body = soup.find("body")
        if not body:
            return None

        for tag in body.find_all(["script", "style", "noscript"]):
            tag.decompose()

        text = body.get_text(separator=" ")

        for pattern in self.PRICE_PATTERNS:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match_str in matches:
                parsed = parse_price(match_str)
                if parsed and parsed < Decimal("100000"):
                    return {"price": parsed}

        return None
