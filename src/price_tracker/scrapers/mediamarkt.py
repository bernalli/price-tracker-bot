"""Mediamarkt scraper — extracts product info from mediamarkt.* (9 EU TLDs).

Strategy: JSON-LD Product/offers (primary) with DOM fallback on the
[data-test=branded-price-*] spans.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, ClassVar, TypedDict
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_currency,
    get_headers,
    in_carousel_context,
    parse_price,
    select_jsonld_offer,
    unwrap_jsonld_graph,
)

if TYPE_CHECKING:
    from decimal import Decimal

logger = logging.getLogger(__name__)


def _default_currency_for_url(url: str) -> str:
    """Map mediamarkt TLD → ISO currency."""
    netloc = urlparse(url).netloc.lower()
    if netloc.endswith(".ch"):
        return "CHF"
    if netloc.endswith(".pl"):
        return "PLN"
    if netloc.endswith(".hu"):
        return "HUF"
    # de / it / es / nl / at / gr → EUR
    return "EUR"


class StrategyResult(TypedDict, total=False):
    price: Decimal
    currency: str
    name: str


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_mediamarkt_html(url: str, client: httpx.AsyncClient) -> httpx.Response:
    headers = get_headers()
    response = await client.get(url, headers=headers, follow_redirects=True)
    return response


class MediamarktScraper(AbstractScraper):
    """Scraper for MediaMarkt product pages across major EU locales."""

    name: ClassVar[str] = "mediamarkt"
    priority: ClassVar[int] = 50
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(
            r"^([\w-]+\.)?mediamarkt\.(de|it|es|nl|hu|at|pl|gr|ch)$",
            re.IGNORECASE,
        ),
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        try:
            response = await _fetch_mediamarkt_html(url, client)
            detect_block_event(status_code=response.status_code, body=response.text, url=url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.debug("Mediamarkt fetch failed for %s: %s", url[:80], e)
            return ProductInfo(error=f"HTTP error: {e}")

        soup = BeautifulSoup(response.text, "lxml")
        info = ProductInfo()

        for strategy_name, strategy_fn in (
            ("jsonld", self._try_jsonld),
            ("dom", self._try_dom),
        ):
            try:
                result = strategy_fn(soup)
                if result and result.get("price") is not None and info.price is None:
                    info.price = result["price"]
                    if result.get("currency") and info.currency is None:
                        info.currency = result["currency"]
                    if result.get("name") and info.name is None:
                        info.name = result["name"]
                    logger.debug("mediamarkt price via %s: %s", strategy_name, info.price)
                    if info.price is not None and info.name is not None:
                        break
            except (ValueError, KeyError, AttributeError) as e:
                logger.debug("mediamarkt strategy %s error: %s", strategy_name, e)
                continue

        if info.name is None:
            title_tag = soup.find("title")
            if isinstance(title_tag, Tag):
                title_text = title_tag.get_text(strip=True)
                if title_text:
                    info.name = title_text[:200]

        if info.currency is None:
            info.currency = detect_currency(str(info.price or "")) or _default_currency_for_url(url)

        if info.price is None:
            info.error = "Price not found in page"

        return info

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
            # MediaMarkt nests the Product inside a BuyAction (`object`), and
            # other sites use `@graph`; unwrap_jsonld_graph flattens both.
            for item in unwrap_jsonld_graph(data):
                type_val = item.get("@type", "")
                type_str = " ".join(type_val) if isinstance(type_val, list) else str(type_val)
                if "Product" not in type_str:
                    continue
                selected = select_jsonld_offer(item.get("offers"))
                if selected is None:
                    continue
                parsed, currency = selected
                result: StrategyResult = {
                    "price": parsed,
                    "currency": currency or "EUR",
                }
                name = item.get("name")
                if isinstance(name, str) and name:
                    result["name"] = name[:200]
                return result
        return None

    @staticmethod
    def _find_whole_value_span(soup: BeautifulSoup) -> Tag | None:
        """Locate the main product's whole-value price span, old or new markup."""
        selector = '[data-test="branded-price-whole-value"]'
        container = soup.select_one('[data-test="mms-pdp-product-price"]')
        if isinstance(container, Tag):
            whole = container.select_one(selector)
            if isinstance(whole, Tag):
                return whole
        for candidate in soup.select(selector):
            if not in_carousel_context(candidate):
                return candidate
        return None

    def _try_dom(self, soup: BeautifulSoup) -> StrategyResult | None:
        """Read the price off the [data-test=branded-price-*] spans.

        Accepts both page shapes. Older pages wrap the price in
        [data-test=mms-pdp-product-price]; that wrapper is gone from current
        ones, which silently disabled this whole fallback. So the wrapper is
        used as a scope when present — it pins the main product's price — and
        otherwise the spans are located in the document, skipping any that sit
        inside a recommendations carousel.
        """
        whole = self._find_whole_value_span(soup)
        if whole is None:
            return None
        # Siblings of the whole-value span, when present: "259," + "–" + " €".
        scope = whole.parent if isinstance(whole.parent, Tag) else soup
        # The whole-value span carries the decimal separator and may carry a
        # thousands separator too ("1.299,"), so join on it rather than
        # stripping it — "1.299" + "." + "99" would not parse.
        text = whole.get_text(strip=True)

        decimals = ""
        decimal_el = scope.select_one('[data-test="branded-price-decimal-value"]')
        if isinstance(decimal_el, Tag):
            candidate = decimal_el.get_text(strip=True)
            # A round price renders the decimals as a dash ("259,–"), not digits.
            if candidate.isdigit():
                decimals = candidate
        if decimals:
            separator = "" if text.endswith((",", ".")) else ","
            text = f"{text}{separator}{decimals}"
        else:
            text = text.rstrip(",.")

        parsed = parse_price(text)
        if parsed is None:
            return None
        result: StrategyResult = {"price": parsed}

        currency_el = scope.select_one('[data-test="branded-price-currency"]')
        if isinstance(currency_el, Tag):
            currency = detect_currency(currency_el.get_text(strip=True))
            if currency:
                result["currency"] = currency
        return result
