"""Shopify scraper — uses the public /products/{handle}.json API.

Works on any Shopify-powered store without needing JS rendering.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import ClassVar
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from price_tracker.core.exceptions import BlockEvent, HTTPBlockStatus, ListingGone
from price_tracker.core.http_client import public_request
from price_tracker.core.identity import RequestedIdentity
from price_tracker.core.money import MAX_AMOUNT, Money, normalize_currency_code
from price_tracker.core.pricegrammar import Unreadable, select_offer
from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_currency,
    detect_listing_gone,
    get_headers,
    parse_price,
    retry_after_header,
    unwrap_jsonld_graph,
)
from price_tracker.core.structured_data import StructureError, decode_json_strict

logger = logging.getLogger(__name__)


_SHOPIFY_PRODUCT_PATH_RE = re.compile(r"(?:^|/)products/[a-z0-9\-_]+", re.IGNORECASE)


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_shopify_response(url: str, client: httpx.AsyncClient) -> httpx.Response:
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
    return response


def _is_product_path(url: httpx.URL | str) -> bool:
    """True when the URL path contains a Shopify-style /products/<slug> segment.

    Used to reject home/collection redirects that would otherwise let the HTML
    fallback parse a random price out of an unrelated page.
    """
    path = url.path if isinstance(url, httpx.URL) else urlparse(str(url)).path
    return bool(_SHOPIFY_PRODUCT_PATH_RE.search(path))


_SCHEMA_ORG_TYPE_PREFIXES = ("https://schema.org/", "http://schema.org/")


def _schema_type_names(node: Mapping[str, object]) -> frozenset[str]:
    """Lower-cased ``@type`` names of a JSON-LD node, schema.org prefix stripped."""
    raw = node.get("@type")
    if raw is None:
        return frozenset()
    values = raw if isinstance(raw, list) else [raw]
    names: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        for prefix in _SCHEMA_ORG_TYPE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        if text:
            names.add(text.casefold())
    return frozenset(names)


def _url_matches_requested(value: object, requested: RequestedIdentity) -> bool:
    """True when a JSON-LD ``url`` field (a string, or a list of strings) is the
    requested product. A list matches only when EVERY entry resolves to the
    requested URL — a list naming the requested product alongside a foreign one
    is a mismatch, not a match."""
    if isinstance(value, str):
        return requested.resolve(value) == requested.url
    if isinstance(value, list) and value:
        resolved = {requested.resolve(v) for v in value if isinstance(v, str)}
        return resolved == {requested.url}
    return False


def _requested_variant_id(url: str) -> str | None:
    """The ``variant`` query parameter of the requested URL, if present."""
    for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if key == "variant" and value:
            return value
    return None


def _without_variant_param(url: str) -> str:
    """``url`` with any ``variant`` query parameter removed.

    A ``ProductGroup`` identifies the product family, not one SKU: its own ``url``
    never carries a ``?variant=`` selector, so the group-identity comparison must
    not either — only the per-variant Offer URLs do.
    """
    parsed = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "variant"]
    return parsed._replace(query=urlencode(kept)).geturl()


def _embedded_variant_price(value: object) -> Decimal | None:
    """Price of one variant of the theme-embedded product JSON, or ``None``.

    Liquid's ``json`` filter renders a variant price as integer cents
    (``19900`` is 199.00), unlike the ``.json`` API's decimal strings; a theme
    that formats the price itself writes a decimal string, read as it is.
    Anything else is not read: a bool, a float (cents or units is a guess), zero,
    a negative or out-of-range number, a container. No price is recoverable; a
    wrong one is pushed to the user.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        amount = Decimal(value).scaleb(-2)
        return amount if amount <= MAX_AMOUNT else None
    if isinstance(value, str):
        return parse_price(value)
    return None


def _embedded_documents(html: str) -> Iterator[str]:
    """Yield whole theme documents, retaining malformed or truncated sources for rejection."""
    marker = re.compile(r"\b(?:var\s+meta|(?:window\.)?ShopifyAnalytics\.meta)\s*=\s*(?=\{)")
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if str(script.get("id", "")).lower().startswith("productjson"):
            yield raw
            continue
        assignments = list(marker.finditer(raw))
        if not assignments:
            # Analytics, pixel and app scripts mention "product" too: only a
            # recognised theme-payload assignment is read, anything else is skipped.
            continue
        for match in assignments:
            start = match.end()
            depth = 0
            in_string = False
            escaped = False
            end = len(raw)
            for pos in range(start, len(raw)):
                char = raw[pos]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                elif char == '"':
                    in_string = True
                elif char in "[{":
                    depth += 1
                elif char in "]}":
                    depth -= 1
                    if depth == 0:
                        tail = raw[pos + 1 :].lstrip()
                        if not tail or tail.startswith(";"):
                            end = pos + 1
                        break
            yield raw[start:end]


def _embedded_id(value: object) -> str | None:
    """Canonical positive Shopify identifier, excluding booleans and empty IDs."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return value.lstrip("0") or None
    return None


def _variant_offer_id(variant: Mapping[str, object]) -> str | None:
    """The variant's identifier: the ``variant`` query param of its own Offer URL
    (the common Shopify/Hydrogen shape — each variant's Offer echoes the storefront
    URL with ``?variant=<id>``), else its ``sku``."""
    offers = variant.get("offers")
    offer = offers[0] if isinstance(offers, list) and offers else offers
    if isinstance(offer, Mapping):
        offer_url = offer.get("url")
        if isinstance(offer_url, str):
            for key, value in parse_qsl(urlparse(offer_url).query, keep_blank_values=True):
                if key == "variant" and value:
                    return value
    sku = variant.get("sku")
    if isinstance(sku, str) and sku.strip():
        return sku.strip()
    if isinstance(sku, int) and not isinstance(sku, bool):
        return str(sku)
    return None


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
    response = await public_request(client, "GET", json_url, headers=headers)
    # Don't raise_for_status here — caller handles 403 specially.
    return response


class ShopifyScraper(AbstractScraper):
    """Scraper for Shopify-powered stores using the public JSON API."""

    name: ClassVar[str] = "shopify"
    priority: ClassVar[int] = 80
    # Shopify uses content/path-based detection, not domain regex
    domain_patterns: ClassVar[list[re.Pattern[str]]] = []

    # Known Shopify domains (extend as discovered)
    # Seeded with well-known Shopify storefronts. It is a shortcut, not the
    # contract: any URL carrying a /products/<handle> path is handled anyway,
    # so a store missing from here still resolves to this scraper.
    KNOWN_SHOPIFY_DOMAINS: ClassVar[set[str]] = {
        "allbirds.com",
        "gymshark.com",
        "colourpop.com",
        "fashionnova.com",
        "kith.com",
        "bombas.com",
        "brooklinen.com",
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
            try:
                result = await self._try_json_api(json_url, client)
            except HTTPBlockStatus as json_block:
                if json_block.status != 429:
                    raise
                return await self._scrape_page_after_json_429(url, client, json_block)
            if result and result.price is not None:
                if result.currency:
                    # The variant carried its own currency: the price and its
                    # currency come from the same payload, and the store sees
                    # one request instead of two.
                    return result
                # Detect currency from HTML page (JSON-LD, OG tags). This fetch is
                # enrichment ONLY: the price is already in hand. A challenge/block
                # marker in the HTML must NOT discard a valid scrape (it falsely
                # quarantined three Shopify stores), so swallow BlockEvent and
                # fall back to the default currency.
                try:
                    html = await self._fetch_html(url, client)
                except (BlockEvent, ListingGone):
                    html = None
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
            page_result = self._read_product_page(html, url)
            if page_result is not None:
                return page_result

        return ProductInfo(error="Prezzo non trovato (Shopify)")

    async def _scrape_page_after_json_429(
        self, url: str, client: httpx.AsyncClient, json_block: HTTPBlockStatus
    ) -> ProductInfo:
        """Read the product page after the ``.json`` endpoint answered 429.

        Some stores rate-limit the ``.json`` API per address and path while the
        product page of the same product still answers. The page goes through the
        same validated client and the same HTML chain as the 404 fallback. A
        page that is refused too (403/429, WAF, CAPTCHA) raises its own block,
        naming the page URL: that is the request that was refused last. A page
        that cannot be fetched at all leaves nothing that contradicts the JSON
        block, so that block is raised as it happened.
        """
        logger.info("Shopify JSON answered 429 for %s; reading the product page", url[:80])
        html = await self._fetch_html(url, client)
        if html is None:
            raise json_block
        page_result = self._read_product_page(html, url)
        if page_result is not None:
            return page_result
        return ProductInfo(error="Prezzo non trovato (Shopify)")

    def _read_product_page(self, html: str, url: str) -> ProductInfo | None:
        """Price from a product page's HTML, or ``None`` when nothing was found."""
        if html:
            # A headless (Next.js/Hydrogen) storefront has no legacy embedded Shopify
            # JSON at all: its JSON-LD carries a ProductGroup/hasVariant instead. Once
            # a matching ProductGroup is found, it is authoritative for this page — it
            # always returns a ProductInfo, never None, so neither the whole-document
            # cents scan nor the generic CSS selectors below can override an
            # identity-confirmed read with an unrelated price. It must run BEFORE the
            # cents scan: that scan also matches a numeric JSON-LD ``"price": 129.0``
            # and would read it as 1.29.
            group_result = self._try_product_group(html, url)
            if group_result is not None:
                return group_result

            embedded = self._try_embedded_product_json(html, url)
            if embedded is not None:
                embedded.currency = normalize_currency_code(self._detect_currency_from_html(html))
                return embedded

            result = self._try_cents_price(html)
            if result is not None:
                result.currency = normalize_currency_code(self._detect_currency_from_html(html))
                return result

            soup = BeautifulSoup(html, "lxml")
            css_result = self._try_shopify_selectors(soup)
            if css_result:
                css_result.currency = normalize_currency_code(self._detect_currency_from_html(html))
                return css_result

        return None

    @staticmethod
    async def _fetch_html(url: str, client: httpx.AsyncClient) -> str | None:
        try:
            response = await _fetch_shopify_response(url, client)
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("Shopify HTML fetch failed for %s: %s", url[:60], e)
            return None
        # Surface WAF/CAPTCHA challenge bodies as a BlockEvent → domain quarantine (#7).
        detect_block_event(
            status_code=response.status_code,
            body=response.text,
            url=url,
            retry_after=retry_after_header(response),
        )
        if not _is_product_path(response.url):
            # A removed product does not always answer 404 straight away: measured
            # against a real store, the first request on a cookieless session is
            # redirected to the storefront home and answers 200, and only the next
            # one 404s. Refusing to read a price off the homepage is right, but
            # reporting it as "price not found" made a removal look like a parse
            # failure — the useless message this whole path exists to stop sending.
            # Landing on a *different* product is a different thing (a reshuffled
            # URL, guarded by the handle comparison on the JSON path) and is left
            # to the caller.
            logger.info(
                "Shopify redirected away from the product path: %s -> %s",
                url[:80],
                str(response.url)[:80],
            )
            if _is_product_path(url):
                raise ListingGone(status=404, url=url)
            # The tracked URL was not a product page to begin with (a collection,
            # say): landing elsewhere says nothing about a listing being removed.
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

    @staticmethod
    def _extract_requested_handle(json_url: str) -> str | None:
        """Return the product handle from a /products/<handle>.json URL, if present."""
        match = re.search(r"/products/([a-z0-9\-_]+)\.json", json_url, re.IGNORECASE)
        return match.group(1) if match else None

    async def _try_json_api(self, json_url: str, client: httpx.AsyncClient) -> ProductInfo | None:
        """Fetch product data from Shopify JSON API."""
        try:
            logger.debug("Shopify JSON API: fetching %s", json_url)
            response = await _fetch_shopify_json(json_url, client)
            logger.debug("Shopify JSON API: status %s", response.status_code)

            if response.status_code == 403:
                # Try curl_cffi fallback before conceding the block: it can bypass
                # some WAFs the primary client can't. Only when it ALSO fails do we
                # surface the block — until then this must never become a silent
                # "no data": block detection precedes parsing (#16).
                data = await self._fetch_json_via_curl_cffi(json_url)
                if data is None:
                    detect_block_event(
                        status_code=response.status_code,
                        body=response.text,
                        url=json_url,
                        retry_after=retry_after_header(response),
                    )
                    return None  # pragma: no cover - detect_block_event always raises here
            elif response.status_code == 404:
                # Not a block: a headless (Next.js/Hydrogen) storefront has no static
                # .json endpoint at all. Fall through to the HTML fallback.
                return None
            elif response.status_code != 200:
                # 429 and any other non-200 status (a WAF/CAPTCHA body can also ride
                # on a 200) must surface as a BlockEvent when it is one, never as a
                # swallowed None (#16, JSON branch never checked this before).
                detect_block_event(
                    status_code=response.status_code,
                    body=response.text,
                    url=json_url,
                    retry_after=retry_after_header(response),
                )
                return None
            else:
                detect_block_event(
                    status_code=response.status_code,
                    body=response.text,
                    url=json_url,
                    retry_after=retry_after_header(response),
                )
                data = response.json()

            product = data.get("product", {})
            if not product:
                logger.warning("Shopify JSON API: no 'product' key in response")
                return None

            # Guard against a dead-slug redirect landing on a DIFFERENT product:
            # the JSON path follows redirects, so verify the returned handle still
            # matches the one we requested (the HTML path guards via _is_product_path).
            requested_handle = self._extract_requested_handle(json_url)
            returned_handle = product.get("handle")
            if (
                requested_handle
                and returned_handle
                and requested_handle.lower() != str(returned_handle).lower()
            ):
                logger.info(
                    "Shopify JSON redirected to a different product (%s != %s) — rejecting",
                    requested_handle,
                    returned_handle,
                )
                return None

            name = product.get("title")

            variants = product.get("variants", [])
            logger.debug("Shopify JSON API: %s, %d variants", name, len(variants))
            declares_availability = any(isinstance(v, dict) and "available" in v for v in variants)
            price: Decimal | None = None
            currency: str | None = None
            available = True
            # Prefer variants the shop declares purchasable: sold-out products
            # often keep a placeholder price on the first variant (#33).
            for variant in variants:
                # Absent key = purchasable: only an explicit False marks sold-out.
                if declares_availability and not variant.get("available", True):
                    continue
                variant_price = variant.get("price")
                if variant_price:
                    parsed = parse_price(str(variant_price))
                    logger.debug("Shopify variant price: %s -> %s", variant_price, parsed)
                    if parsed:
                        price = parsed
                        currency = normalize_currency_code(variant.get("price_currency"))
                        break

            if price is None and declares_availability:
                # No purchasable variant: report the first priced one, but
                # flag the product as unavailable.
                for variant in variants:
                    variant_price = variant.get("price")
                    if variant_price:
                        parsed = parse_price(str(variant_price))
                        if parsed:
                            price = parsed
                            currency = normalize_currency_code(variant.get("price_currency"))
                            available = False
                            break

            if price is None:
                return None

            return ProductInfo(
                name=name,
                price=price,
                # The chosen variant's own currency. None when the payload has no
                # usable ``price_currency``: the caller then reads it from the HTML.
                currency=currency,
                available=available,
            )

        except (json.JSONDecodeError, httpx.HTTPError, ValueError, KeyError, AttributeError) as e:
            logger.debug("Shopify JSON API error for %s: %s", json_url, e)
            return None

    @staticmethod
    async def _fetch_json_via_curl_cffi(json_url: str) -> dict | None:
        """Disabled: this backend cannot bind connections to validated addresses."""
        return None

    @staticmethod
    def _try_product_group(html: str, url: str) -> ProductInfo | None:
        """Fallback for a headless storefront: JSON-LD ``ProductGroup``/``hasVariant``.

        A JSON API 404 is not a block on a headless (Next.js/Hydrogen) storefront:
        the static ``.json`` endpoint simply doesn't exist there, but the
        server-rendered page carries a ``ProductGroup`` node whose ``url`` echoes
        the requested product and whose ``hasVariant`` list carries one ``Offer``
        per SKU.

        Returns ``None`` only when no ``ProductGroup`` matching the requested
        identity was found at all — letting the caller still try its other HTML
        heuristics. Once a matching node is found it is authoritative: every other
        exit returns a ``ProductInfo`` (with or without a price), so a wrong or
        foreign price can never be picked afterwards.
        """
        try:
            requested = RequestedIdentity.from_url(_without_variant_param(url))
        except ValueError:
            return None

        soup = BeautifulSoup(html, "lxml")
        groups: list[Mapping[str, object]] = []
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or script.get_text(strip=True)
            if not raw:
                continue
            try:
                data = decode_json_strict(raw)
            except StructureError:
                continue
            for node in unwrap_jsonld_graph(data):
                if "productgroup" not in _schema_type_names(node):
                    continue
                if _url_matches_requested(node.get("url"), requested):
                    groups.append(node)

        if not groups:
            return None

        # Several nodes owned by the requested product must agree; the first one
        # does not win (same rule as owned sources in core.anchoring).
        results = [ShopifyScraper._read_product_group(group, url) for group in groups]
        first = results[0]
        if any((r.price, r.currency) != (first.price, first.currency) for r in results[1:]):
            return ProductInfo(
                error="Prezzo ambiguo (Shopify): ProductGroup discordanti per lo stesso prodotto"
            )
        return first

    @staticmethod
    def _read_product_group(group: Mapping[str, object], url: str) -> ProductInfo:
        """Price of one identity-confirmed ``ProductGroup``; never ``None``."""
        name = group.get("name") if isinstance(group.get("name"), str) else None

        variants_raw = group.get("hasVariant")
        variants_list = variants_raw if isinstance(variants_raw, list) else [variants_raw]
        variants = [v for v in variants_list if isinstance(v, Mapping)]
        if not variants:
            return ProductInfo(error="Prezzo non trovato (Shopify): ProductGroup senza varianti")

        requested_variant_id = _requested_variant_id(url)
        if requested_variant_id is not None:
            matches = [v for v in variants if _variant_offer_id(v) == requested_variant_id]
            if len(matches) > 1:
                return ProductInfo(
                    error=(
                        "Prezzo ambiguo (Shopify): variante "
                        f"{requested_variant_id!r} presente più volte"
                    )
                )
            if not matches:
                return ProductInfo(
                    error=(
                        "Prezzo non trovato (Shopify): variante "
                        f"{requested_variant_id!r} non trovata"
                    )
                )
            selected = select_offer(matches[0].get("offers"))
            if isinstance(selected, Unreadable):
                return ProductInfo(
                    error=(
                        "Prezzo non trovato (Shopify): prezzo variante illeggibile "
                        f"({selected.reason})"
                    )
                )
            return ProductInfo(name=name, price=selected.amount, currency=selected.currency)

        readable: list[Money] = []
        for variant in variants:
            selected = select_offer(variant.get("offers"))
            if isinstance(selected, Money):
                readable.append(selected)
        if not readable:
            return ProductInfo(
                error="Prezzo non trovato (Shopify): nessuna variante con prezzo leggibile"
            )
        if len(set(readable)) > 1:
            return ProductInfo(
                error=("Prezzo ambiguo (Shopify): varianti a prezzi diversi, nessuna selezionata")
            )
        chosen = readable[0]
        return ProductInfo(name=name, price=chosen.amount, currency=chosen.currency)

    def _try_embedded_product_json(self, html: str, url: str) -> ProductInfo | None:
        """Select one complete, identity-bound product; rejection is terminal."""
        rejected = ProductInfo(error="Unreadable or ambiguous embedded Shopify product")
        try:
            requested = RequestedIdentity.from_url(_without_variant_param(url))
        except ValueError:
            return rejected
        handles = [
            unquote(h).casefold()
            for h in re.findall(r"/products/([^/]+)(?:/|$)", urlparse(url).path)
        ]
        selectors = [
            v for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True) if k == "variant"
        ]
        if selectors and (len(selectors) != 1 or _embedded_id(selectors[0]) is None):
            return rejected
        selected_id = _embedded_id(selectors[0]) if selectors else None
        products: list[Mapping[str, object]] = []
        saw_product = False
        for raw in _embedded_documents(html):
            try:
                data = decode_json_strict(raw)
            except StructureError:
                return rejected
            if not isinstance(data, Mapping):
                return rejected
            if "product" not in data and "variants" not in data:
                continue
            saw_product = True
            product = data.get("product", data)
            if not isinstance(product, Mapping):
                return rejected
            # A numeric product ID alone cannot establish ownership of a slug URL.
            ownership: list[bool] = []
            if "handle" in product:
                handle = product["handle"]
                ownership.append(
                    bool(handles)
                    and isinstance(handle, str)
                    and unquote(handle).casefold() == handles[0]
                )
            if "url" in product:
                ownership.append(_url_matches_requested(product["url"], requested))
            if not ownership or (any(ownership) and not all(ownership)):
                return rejected
            if all(ownership):
                products.append(product)
        if not saw_product:
            return None
        if len(products) != 1:
            return rejected
        product = products[0]
        variants = product.get("variants")
        if not isinstance(variants, list) or not variants:
            return rejected
        by_id: dict[str, Mapping[str, object]] = {}
        for variant in variants:
            if not isinstance(variant, Mapping):
                return rejected
            identity = _embedded_id(variant.get("id"))
            if identity is None or identity in by_id:
                return rejected
            by_id[identity] = variant
        if selected_id is not None:
            if selected_id not in by_id:
                return rejected
            price = _embedded_variant_price(by_id[selected_id].get("price"))
        else:
            prices = {_embedded_variant_price(v.get("price")) for v in by_id.values()}
            prices.discard(None)
            if len(prices) != 1:
                return rejected
            price = prices.pop()
        if price is None:
            return rejected
        name = product.get("title")
        return ProductInfo(name=name if isinstance(name, str) else None, price=price)

    @staticmethod
    def _try_cents_price(html: str) -> ProductInfo | None:
        """Last-resort Shopify-style price in cents anywhere in the page (2999 = 29.99)."""
        info = ProductInfo()
        cents_patterns = [
            r'"price"\s*:\s*(\d{3,7})(?=\s*[,}\]])',
            r'"price_min"\s*:\s*(\d{3,7})(?=\s*[,}\]])',
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
