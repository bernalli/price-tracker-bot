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
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from price_tracker.core.exceptions import BlockEvent, ListingGone
from price_tracker.core.http_client import public_request
from price_tracker.core.identity import RequestedIdentity, check_echoes, normalize_url
from price_tracker.core.page_image import (
    ImageDocument,
    ImagePageHTML,
    PriceImageBinding,
    extract_page_image,
)
from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_currency,
    detect_listing_gone,
    get_headers,
    in_carousel_context,
    jsonld_offer_availability,
    parse_price,
    retry_after_header,
)
from price_tracker.scrapers.evidence import offer_evidence, offer_urls_match, price_currency

logger = logging.getLogger(__name__)


def _log_host(url: str) -> str:
    """Keep URL paths, userinfo, and query strings out of logs."""
    try:
        return urlparse(url).hostname or "unknown"
    except ValueError:
        return "unknown"


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

# Inline-JS extraction (#53): price-bearing keys, and identity keys marking a
# JSON object as the product itself rather than analytics/related-item data.
_JS_PRICE_KEYS: tuple[str, ...] = (
    "price",
    "salePrice",
    "sale_price",
    "current_price",
    "finalPrice",
    "selling_price",
)
_JS_IDENTITY_KEYS: tuple[str, ...] = ("name", "title", "sku", "productId", "product_id")


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_generic_html(url: str, client: httpx.AsyncClient) -> str:
    """Single GET attempt with browser headers. Tenacity handles retries."""
    headers = get_headers()
    response = await public_request(client, "GET", url, headers=headers)
    # Surface 403/429 (and WAF/CAPTCHA bodies) as a BlockEvent BEFORE
    # raise_for_status, so the scheduler quarantines the domain instead of
    # recording a generic failure (#16). with_retry never retries BlockEvents.
    detect_block_event(
        status_code=response.status_code,
        body=response.text,
        url=url,
        retry_after=retry_after_header(response),
    )
    detect_listing_gone(status_code=response.status_code, url=url)
    response.raise_for_status()
    return ImagePageHTML(response.text, str(response.url))


async def _fetch_with_curl_cffi(url: str) -> str | None:
    """Disabled: this backend cannot bind connections to validated addresses."""
    return None


class GenericScraper(AbstractScraper):
    """Catch-all scraper running multi-strategy extraction."""

    name: ClassVar[str] = "generic"
    priority: ClassVar[int] = 0
    domain_patterns: ClassVar[list[re.Pattern[str]]] = []

    # Extended list of selectors found across many e-commerce platforms
    PRICE_SELECTORS: ClassVar[list[str]] = [
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
        ".price__current",
        ".price-item--sale",
        ".price-item--regular",
        # Broad data-* selectors LAST: page-wide attribute hits match unrelated
        # widgets (carousels, upsells), so product-specific class selectors must
        # win and matches are only trusted inside a product container (#26).
        "[data-price]",
        "[data-product-price]",
        "[data-current-price]",
        "[data-sale-price]",
    ]

    # Selectors so broad that a match is accepted only inside a product container (#26)
    BROAD_DATA_SELECTORS: ClassVar[frozenset[str]] = frozenset(
        {"[data-price]", "[data-product-price]", "[data-current-price]", "[data-sale-price]"}
    )

    _PRODUCT_SCOPE_RE: ClassVar[re.Pattern[str]] = re.compile(r"product|main", re.IGNORECASE)

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
        # Keep the legacy fallback contract; its unbound backend is disabled.
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
            except ListingGone:
                if not html:
                    raise
                logger.debug(
                    "Generic httpx fetch found a removed listing for %s; "
                    "keeping the curl_cffi HTML",
                    _log_host(url),
                )
            except (httpx.HTTPError, ValueError) as e:
                logger.debug(
                    "Generic httpx fetch failed for %s: %s", _log_host(url), type(e).__name__
                )
                # Keep curl_cffi result (if any)
        if not html:
            if curl_block is not None:
                # Both paths failed and curl_cffi saw a hard block (403/429):
                # surface it so the scheduler quarantines the domain (#16).
                raise curl_block
            return ProductInfo(error="Impossibile caricare la pagina", stock_evidence=False)

        # Surface WAF/CAPTCHA challenge bodies (often served with HTTP 200) as a
        # BlockEvent so the scheduler quarantines the domain (#7).
        detect_block_event(status_code=200, body=html, url=url)

        soup = BeautifulSoup(html, "lxml")
        for tag in list(soup.find_all()):
            if tag.parent is not None and in_carousel_context(tag):
                tag.decompose()
        info = ProductInfo(stock_evidence=False)
        image_document = ImageDocument.parse(soup)
        page_url = getattr(html, "final_url", url)

        try:
            requested = RequestedIdentity.from_url(url)
        except ValueError:
            normalized = normalize_url(url)
            if normalized is None:
                return ProductInfo(error="Identità del prodotto illeggibile", stock_evidence=False)
            requested = RequestedIdentity(normalized, urlparse(normalized).hostname or "")
        final_url = page_url
        echoes = soup.select('link[rel="canonical"], meta[property="og:url"]')
        canonical = next((str(t.get("href")) for t in echoes if t.name == "link"), None)
        if not check_echoes(requested, final_url=final_url, canonical=canonical).ok:
            return ProductInfo(
                error="Identità del prodotto non corrispondente", stock_evidence=False
            )
        for tag in echoes:
            echo = str(tag.get("href") or tag.get("content") or "")
            if requested.resolve(echo, page_url) != requested.url:
                return ProductInfo(
                    error="Identità del prodotto non corrispondente", stock_evidence=False
                )

        strategies = [
            ("JSON-LD", lambda s: self._try_json_ld_bound(s, url, page_url, image_document)),
            ("Microdata", lambda s: self._try_microdata(s, url=url)),
            ("OpenGraph", self._try_opengraph),
            ("RDFa", self._try_rdfa),
            ("Meta tags", self._try_meta_tags),
            ("CSS selectors", self._try_css_selectors),
            ("Data attributes", self._try_data_attributes),
            ("JS product data", lambda s: self._try_js_product_data(s, url=url)),
            ("Regex", self._try_regex),
        ]

        successful_strategy: str | None = None
        for strategy_name, strategy_fn in strategies:
            try:
                result = strategy_fn(soup)
                if result:
                    if result.get("identity_error"):
                        return ProductInfo(
                            error="Identità del prodotto non corrispondente", stock_evidence=False
                        )
                    if result.get("currency_ambiguous"):
                        return ProductInfo(error="currency_ambiguous", stock_evidence=False)
                    if (
                        info.price is None
                        and result.get("available") is False
                        and result.get("price") is None
                    ):
                        return ProductInfo(name=result.get("name"), available=False)
                    if result.get("price") is not None and info.price is None:
                        currency, ambiguous = price_currency(
                            result.get("price_text"), result.get("currency")
                        )
                        if ambiguous or result.get("currency_ambiguous"):
                            return ProductInfo(error="currency_ambiguous", stock_evidence=False)
                        info.price = result["price"]
                        info.currency = currency
                        info.image_url = result.get("image_url")
                        successful_strategy = strategy_name
                        if isinstance(result.get("available"), bool):
                            info.available = result["available"]
                            info.stock_evidence = True
                        logger.debug("Price found via %s: %s", strategy_name, info.price)
                    if result.get("name") and info.name is None:
                        info.name = result["name"]

                if info.price is not None and info.name is not None:
                    break
            except (ValueError, KeyError, AttributeError) as e:
                logger.debug("Strategy %s error: %s", strategy_name, type(e).__name__)
                continue

        if successful_strategy == "Regex":
            # Heuristic regex on visible text is the last-resort path. Log a
            # warning so operators can spot domains that need a tuned scraper.
            logger.warning(
                "generic.heuristic_fallback: price extracted via regex heuristic for %s",
                _log_host(url),
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

        if successful_strategy in {"OpenGraph", "Meta tags"} and info.price is not None:
            echoes = soup.select('link[rel="canonical"], meta[property="og:url"]')
            if echoes and "variant=" not in url:
                info.image_url = extract_page_image(
                    soup,
                    page_url=page_url,
                    price_binding=PriceImageBinding(
                        image_document,
                        url,
                        (),
                        normalize_url(url),
                        True,
                        price_source="page",
                        page_scope="product",
                    ),
                )
        return info

    def _try_json_ld_bound(
        self, soup: BeautifulSoup, url: str, page_url: str, document: ImageDocument
    ) -> dict | None:
        """Select attributed nodes before reading their prices and images together."""
        try:
            requested = RequestedIdentity.from_url(url)
        except ValueError:
            normalized = normalize_url(url)
            if normalized is None:
                return {"identity_error": True}
            requested = RequestedIdentity(normalized, urlparse(normalized).hostname or "")
        selected: list[tuple[str, dict]] = []
        anonymous: list[tuple[str, dict]] = []
        foreign_product = False
        if not document.valid:
            return {"identity_error": True}
        for ref, node in document.nodes.items():
            kinds = node.get("@type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if not any(
                isinstance(kind, str) and kind.rsplit("/", 1)[-1] in PRODUCT_TYPES for kind in kinds
            ):
                continue
            identifiers: list[object] = []
            for key in ("url", "@id"):
                if key in node:
                    values = node[key] if isinstance(node[key], list) else [node[key]]
                    identifiers.extend(values or [None])
            if identifiers and not all(
                isinstance(raw, str) and requested.resolve(raw) == requested.url
                for raw in identifiers
            ):
                foreign_product = True
                continue
            offers = node.get("offers", node.get("Offers", {}))
            if not offer_urls_match(
                offers, lambda value: requested.resolve(value) == requested.url
            ):
                return {"identity_error": True}
            price = self._extract_price_from_offers(offers)
            if not price:
                for key in ("price", "lowPrice", "highPrice"):
                    if node.get(key) is not None and (amount := parse_price(str(node[key]))):
                        price = {
                            "price": amount,
                            "price_text": str(node[key]),
                            "currency": node.get("priceCurrency"),
                        }
                        break
            if price:
                target = selected if identifiers else anonymous
                target.append((ref, {"name": node.get("name"), **price}))
        if not selected:
            if foreign_product:
                return {"identity_error": True}
            selected = anonymous
        if not selected:
            return None
        first = selected[0][1]
        if any(
            (r.get("price"), r.get("currency"), r.get("available"))
            != (first.get("price"), first.get("currency"), first.get("available"))
            for _, r in selected[1:]
        ):
            return {"identity_error": True}
        first["image_url"] = extract_page_image(
            soup,
            page_url=page_url,
            price_binding=PriceImageBinding(
                document,
                url,
                tuple(ref for ref, _ in selected),
                normalize_url(url),
                True,
            ),
        )
        return first

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
            logger.warning("JSON-LD %s parse error: %s", source, type(e).__name__)
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
        """Pick the representative offer via the shared selector (#27).

        Filters financing/installment entries, never takes the cheapest list
        entry (add-on/warranty trap) nor an AggregateOffer ``lowPrice``, and
        normalises the currency to ISO-4217 (no silent EUR default).
        """
        return offer_evidence(offers)

    # ── Strategy 2: Microdata ──────────────────────────────────────

    def _try_microdata(self, soup: BeautifulSoup, *, url: str | None = None) -> dict | None:
        result: dict = {}
        selected_scope: Tag | BeautifulSoup = soup

        def scope_of(tag: Tag) -> Tag | BeautifulSoup:
            for parent in tag.parents:
                if isinstance(parent, Tag) and (
                    parent.has_attr("itemscope") or parent.get("itemtype")
                ):
                    return parent
            return soup

        def local(scope: Tag | BeautifulSoup, name: str) -> Tag | None:
            return next(
                (tag for tag in scope.find_all(attrs={"itemprop": name}) if scope_of(tag) is scope),
                None,
            )

        def value(tag: Tag | None) -> str | None:
            if tag is None:
                return None
            return str(
                tag.get("content")
                or tag.get("href")
                or tag.get("value")
                or tag.get_text(strip=True)
            )

        def owned(scope: Tag | BeautifulSoup) -> bool:
            if url is None:
                return True
            for node in (scope, *scope.parents):
                if not isinstance(node, Tag):
                    continue
                identifiers = [node.get("itemid"), value(local(node, "url"))]
                if any(
                    raw is not None and normalize_url(urljoin(url, str(raw))) != normalize_url(url)
                    for raw in identifiers
                ):
                    return False
            return True

        for price_el in soup.find_all(attrs={"itemprop": "price"}):
            price_val = price_el.get("content")
            if not price_val:
                price_val = price_el.get("value")
            if not price_val:
                price_val = price_el.get_text(strip=True)
            if price_val:
                parsed = parse_price(str(price_val))
                if parsed:
                    selected_scope = scope_of(price_el)
                    if not owned(selected_scope):
                        return {"identity_error": True}
                    result["price"] = parsed
                    visible = price_el.get_text(strip=True)
                    result["price_text"] = (
                        visible if parse_price(visible) == parsed else str(price_val)
                    )
                    break

        currency_el = local(selected_scope, "priceCurrency")
        if currency_el:
            result["currency"] = value(currency_el)

        result["available"] = jsonld_offer_availability(
            {"availability": value(local(selected_scope, "availability"))}
        )

        if not result.get("price"):
            for availability in soup.find_all(attrs={"itemprop": "availability"}):
                scope = scope_of(availability)
                if not owned(scope):
                    return {"identity_error": True}
                if jsonld_offer_availability({"availability": value(availability)}) is False:
                    return {"available": False}

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
                result["price_text"] = og_price.get("content", "")

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
        result: dict = {"price": parsed, "price_text": str(price_str)}
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
                    result["price_text"] = content
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
                    result["price_text"] = el.get("content", "")

        return result if result.get("price") else None

    # ── Strategy 6: CSS selectors ──────────────────────────────────

    @classmethod
    def _in_product_container(cls, el: Tag) -> bool:
        """True when `el` or an ancestor looks like the main product container (#26).

        Qualifying markers: id/class containing 'product' or 'main', or an
        itemtype containing 'schema.org/Product'. Page-wide ``[data-*]`` price
        hits outside such a container belong to unrelated widgets (carousels,
        upsells) and must not be trusted.
        """
        node: Tag | None = el
        while isinstance(node, Tag):
            id_attr = node.get("id") or ""
            classes = node.get("class") or []
            if isinstance(classes, str):
                classes = [classes]
            if cls._PRODUCT_SCOPE_RE.search(" ".join([str(id_attr), *classes])):
                return True
            if "schema.org/product" in str(node.get("itemtype") or "").lower():
                return True
            node = node.parent
        return False

    def _try_css_selectors(self, soup: BeautifulSoup) -> dict | None:
        for selector in self.PRICE_SELECTORS:
            try:
                elements = soup.select(selector)
            except (ValueError, AttributeError):
                continue
            for el in elements:
                if selector in self.BROAD_DATA_SELECTORS and not self._in_product_container(el):
                    continue
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
                            visible = el.get_text(strip=True)
                            return {
                                "price": parsed,
                                "price_text": visible
                                if parse_price(visible) == parsed
                                else str(data_val),
                            }

                text = el.get_text(strip=True)
                if text:
                    parsed = parse_price(text)
                    if parsed:
                        return {"price": parsed, "price_text": text}
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
                if not self._in_product_container(el):
                    continue
                val = el.get(attr, "")
                parsed = parse_price(str(val))
                if parsed:
                    visible = el.get_text(strip=True)
                    return {
                        "price": parsed,
                        "price_text": visible if parse_price(visible) == parsed else str(val),
                    }

        return None

    # ── Strategy 8: JS embedded product data ───────────────────────

    def _try_js_product_data(self, soup: BeautifulSoup, *, url: str | None = None) -> dict | None:
        # A bare price key or a named object inside analytics/recommendations is
        # not page ownership. Only an explicit product assignment is a page source.
        assignment = re.compile(
            r"(?:\b(?:var|let|const)\s+|\bwindow\.)product(?:Data)?\s*=\s*(?=\{)"
        )
        for script in soup.find_all("script"):
            if script.get("type") == "application/ld+json":
                continue
            raw = script.string or ""
            if len(raw) > 200000:
                continue
            for match in assignment.finditer(raw):
                try:
                    data, _ = json.JSONDecoder().raw_decode(raw[match.end() :])
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    if url and any(
                        not isinstance(data[key], str)
                        or normalize_url(urljoin(url, data[key])) != normalize_url(url)
                        for key in ("url", "@id")
                        if key in data
                    ):
                        return {"identity_error": True}
                    result = self._js_identified_object_price(json.dumps(data))
                    if result:
                        return result
        return None

    def _js_identified_object_price(self, text: str) -> dict | None:
        """Price from a flat JSON object carrying product identity keys (#53)."""
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict) or not any(obj.get(key) for key in _JS_IDENTITY_KEYS):
            return None
        for key in _JS_PRICE_KEYS:
            value = obj.get(key)
            if value is None:
                continue
            parsed = parse_price(str(value))
            if parsed and Decimal("0.50") < parsed < Decimal("100000"):
                return {
                    "price": parsed,
                    "price_text": str(value),
                    "currency": obj.get("currency"),
                    "name": obj.get("name") or obj.get("title"),
                }
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
