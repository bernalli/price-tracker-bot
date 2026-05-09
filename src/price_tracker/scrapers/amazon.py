"""Amazon-specific scraper.

Amazon has aggressive anti-bot measures — this scraper uses targeted
selectors, careful header management, and multiple fallbacks (curl_cffi,
scrapling) when 403/CAPTCHA are returned.
"""

from __future__ import annotations

import json
import logging
import random
import re
from decimal import Decimal
from typing import ClassVar

import httpx
from bs4 import BeautifulSoup

from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    USER_AGENTS,
    AbstractScraper,
    ProductInfo,
    detect_currency,
    get_headers,
    parse_price,
)

logger = logging.getLogger(__name__)


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_amazon_html(
    url: str, client: httpx.AsyncClient, extra_headers: dict[str, str] | None = None
) -> str:
    """Single GET attempt with browser-like headers. Tenacity handles retries."""
    headers = get_headers(extra_headers)
    response = await client.get(url, headers=headers, follow_redirects=True)
    response.raise_for_status()
    return response.text


async def _fetch_with_fresh_client(url: str) -> str | None:
    """Fetch with a brand-new httpx client and minimal headers."""
    ua = random.choice(USER_AGENTS)  # noqa: S311 — non-cryptographic UA rotation
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as fresh:
            response = await fresh.get(url, headers=headers)
            response.raise_for_status()
            return response.text
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("Fresh client retry failed: %s", e)
        return None


async def _fetch_via_curl_cffi(url: str) -> str | None:
    """Last-resort fetch with Chrome JA3/TLS impersonation. Returns None on any failure."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        logger.debug("curl_cffi not available for Amazon fallback")
        return None
    try:
        async with AsyncSession(impersonate="chrome") as session:
            resp = await session.get(url, allow_redirects=True, timeout=30)
            if resp.status_code == 200:
                logger.info("curl_cffi fallback succeeded for %s", url[:60])
                return resp.text
            logger.warning("curl_cffi fallback got %s for %s", resp.status_code, url[:60])
    except (httpx.HTTPError, ValueError, OSError) as e:
        logger.warning("curl_cffi fallback failed for %s: %s", url[:60], e)
    return None


async def _fetch_via_scrapling(url: str) -> str | None:
    """Final fallback via Scrapling (stealth headers). Returns None on any failure."""
    try:
        from scrapling import Fetcher
    except ImportError:
        logger.debug("Scrapling not available for Amazon fallback")
        return None
    try:
        page = Fetcher.get(url, stealthy_headers=True, follow_redirects=True, timeout=30)
        if page.status == 200 and page.text:
            logger.info("Scrapling fallback succeeded for %s", url[:60])
            return page.text
        logger.warning("Scrapling fallback got status %s for %s", page.status, url[:60])
    except (ValueError, OSError, AttributeError) as e:
        logger.warning("Scrapling fallback failed for %s: %s", url[:60], e)
    return None


async def _fetch_amazon_page(
    url: str, client: httpx.AsyncClient, extra_headers: dict[str, str] | None = None
) -> str | None:
    """Fetch Amazon page with retry + fallback chain. Always returns str | None."""
    html: str | None = None
    got_403 = False
    try:
        html = await _fetch_amazon_html(url, client, extra_headers)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            got_403 = True
        logger.warning("HTTP %s for %s after retries", e.response.status_code, url[:60])
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Fetch error for %s: %s", url[:60], e)

    # Fresh-client retry on suspiciously small responses
    if html and len(html) < 80000 and "application/ld+json" not in html:
        logger.info("Amazon response is small (%d chars), retrying with fresh client", len(html))
        fresh_html = await _fetch_with_fresh_client(url)
        if fresh_html and len(fresh_html) > len(html):
            logger.info("Fresh client got %d chars (vs %d)", len(fresh_html), len(html))
            html = fresh_html

    if html:
        return html

    if got_403:
        html = await _fetch_via_curl_cffi(url)
        if html:
            return html
        html = await _fetch_via_scrapling(url)
        if html:
            return html

    return None


class AmazonScraper(AbstractScraper):
    """Amazon scraper across all major locales (.it, .com, .de, .co.uk, ...)."""

    name: ClassVar[str] = "amazon"
    priority: ClassVar[int] = 100
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"^(www\.)?amazon\.(com|it|de|co\.uk|fr|es|nl|pl|se|ca|com\.au|co\.jp)$"),
        re.compile(r"^(www\.)?amzn\.(eu|to)$"),
    ]

    # Price selectors in priority order
    PRICE_SELECTORS: ClassVar[list[str]] = [
        ".priceToPay .a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        "span.a-price .a-offscreen",
        ".apexPriceToPay .a-offscreen",
        "#dealprice_feature_div .a-offscreen",
        "#priceblock_dealprice",
        "#priceblock_ourprice",
        "#priceblock_saleprice",
        "#price_inside_buybox",
        "#kindle-price",
        "#digital-list-price",
        ".offer-price",
        "#newBuyBoxPrice",
        "#price",
        ".a-price-whole",
    ]

    NAME_SELECTORS: ClassVar[list[str]] = [
        "#productTitle",
        "#title",
        "h1.product-title-word-break",
        "#btAsinTitle",
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        # Resolve Amazon short links to full URL
        if "amzn.eu" in url or "amzn.to" in url:
            try:
                resp = await client.head(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    follow_redirects=True,
                )
                if resp.status_code == 200 and "amazon" in str(resp.url):
                    url = str(resp.url)
                    logger.info("Resolved Amazon short link to: %s", url[:80])
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("Failed to resolve Amazon short link: %s", e)

        extra_headers = {
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.google.com/",
        }

        html = await _fetch_amazon_page(url, client, extra_headers=extra_headers)
        if not html:
            return ProductInfo(error="Impossibile caricare la pagina Amazon")

        soup = BeautifulSoup(html, "lxml")
        info = ProductInfo()

        # Check for captcha page
        if soup.find("form", action=re.compile(r"validateCaptcha")):
            return ProductInfo(error="Amazon ha richiesto un CAPTCHA — riprovo più tardi")

        # Check for "currently unavailable"
        unavail = soup.find(id="availability")
        if unavail and "non disponibile" in unavail.get_text().lower():
            info.available = False

        # Extract price — CSS selectors first (buybox = real price for Amazon)
        css_price = self._extract_price(soup)
        ld_price = self._try_json_ld_price(soup)

        # Prefer CSS, but cross-check with JSON-LD: when CSS differs >2x from
        # Product.offers.price, CSS is likely an installment/rata-mensile value.
        if css_price is not None and ld_price is not None and ld_price > 0:
            ratio = css_price / ld_price
            if ratio < Decimal("0.5") or ratio > Decimal("2"):
                logger.warning(
                    "Amazon price mismatch: CSS=%s vs JSON-LD=%s (ratio=%.2f) for %s "
                    "— trusting JSON-LD (likely installment/variant parse bug)",
                    css_price,
                    ld_price,
                    ratio,
                    url[:60],
                )
                info.price = ld_price
            else:
                info.price = css_price
        else:
            info.price = css_price if css_price is not None else ld_price

        # Extract name
        info.name = self._extract_name(soup)

        info.currency = detect_currency(str(info.price or ""))

        # Log coupon presence for debugging
        coupon = self._detect_coupon(soup)
        if coupon:
            logger.info("Amazon coupon detected for %s: %s", url[:60], coupon)

        # Detect seller and condition
        seller_name, _is_sold_by_amazon = self._detect_seller(soup)
        info.seller = seller_name if seller_name else None
        info.condition = self._detect_condition(soup, seller_name=seller_name)

        # If buybox shows used/renewed, try to find the new price and override
        if info.condition != "new":
            new_price = self._extract_new_price(soup)
            if new_price:
                logger.info(
                    "Buybox is %s at %s, new price available at %s — using new price",
                    info.condition,
                    info.price,
                    new_price,
                )
                info.price = new_price
                info.condition = "new"
                info.seller = None

        if info.price is None:
            info.error = "Prezzo non trovato (prodotto non disponibile?)"

        return info

    def _extract_price(self, soup: BeautifulSoup) -> Decimal | None:
        # Strategy 1: Target the main buy box directly (most reliable)
        buybox_containers = [
            "#corePrice_desktop",
            "#corePriceDisplay_desktop_feature_div",
            "#corePrice_feature_div",
            "#apex_desktop",
            "#buyBoxInner",
            "#desktop_buybox",
        ]
        for container_sel in buybox_containers:
            container = soup.select_one(container_sel)
            if not container:
                continue
            for sel in [
                ".priceToPay .a-offscreen",
                ".apexPriceToPay .a-offscreen",
                ".a-price:not(.a-text-price) .a-offscreen",
            ]:
                for el in container.select(sel):
                    # Skip if this price is in an installment/pay-later sub-widget
                    skip = False
                    for parent in el.parents:
                        if parent is container:
                            break
                        pid = (parent.get("id") or "").lower()
                        pcls = " ".join(parent.get("class", [])).lower()
                        dfn = (parent.get("data-feature-name") or "").lower()
                        if any(
                            k in pid
                            for k in (
                                "installment",
                                "monthly",
                                "paylater",
                                "credit",
                                "cofidis",
                                "pagolight",
                                "subscribeandsave",
                            )
                        ):
                            skip = True
                            break
                        if any(
                            k in pcls
                            for k in (
                                "installment",
                                "monthly",
                                "paylater",
                                "pay-later",
                                "credit-option",
                            )
                        ):
                            skip = True
                            break
                        if any(
                            k in dfn
                            for k in (
                                "installment",
                                "monthly",
                                "paylater",
                                "credit",
                            )
                        ):
                            skip = True
                            break
                    if skip:
                        continue
                    parsed = parse_price(el.get_text(strip=True))
                    if parsed:
                        logger.debug("Amazon price from %s > %s: %s", container_sel, sel, parsed)
                        return parsed

        # Strategy 2: Broad selectors (fallback)
        skip_parent_ids = (
            "olp-",
            "aod-",
            "other-seller",
            "snsPrice",
            "installment",
            "monthlyPayment",
            "monthly_payment",
            "monthlyPricing",
            "creditOption",
            "paylater",
            "pay-later",
            "financeOffer",
            "amazonCredit",
            "amazonPayLater",
            "cofidis",
            "pagolight",
            "subscribeAndSave",
            "sims_",
            "similarities_",
            "sp_detail",
            "bundle_",
            "askInlineWidget",
        )
        skip_parent_classes = (
            "a-text-price",
            "olp-",
            "aod-",
            "installment",
            "monthly",
            "pay-later",
            "paylater",
            "credit-option",
            "finance-offer",
        )
        for selector in self.PRICE_SELECTORS:
            elements = soup.select(selector)
            for el in elements:
                skip = False
                for parent in el.parents:
                    pid = (parent.get("id") or "").lower()
                    pcls = " ".join(parent.get("class", [])).lower()
                    dfn = (parent.get("data-feature-name") or "").lower()
                    if any(s.lower() in pid for s in skip_parent_ids):
                        skip = True
                        break
                    if any(s.lower() in pcls for s in skip_parent_classes):
                        skip = True
                        break
                    if any(
                        k in dfn
                        for k in (
                            "installment",
                            "monthly",
                            "paylater",
                            "credit",
                        )
                    ):
                        skip = True
                        break
                if skip:
                    continue

                text = el.get_text(strip=True)
                if not text:
                    continue

                classes = el.get("class") or []
                if "a-price-whole" in classes:
                    whole = text.rstrip(".,")
                    fraction_el = el.find_next_sibling(class_="a-price-fraction")
                    if fraction_el:
                        fraction = fraction_el.get_text(strip=True)
                        text = f"{whole},{fraction}"
                    else:
                        text = whole

                parsed = parse_price(text)
                if parsed:
                    return parsed

        return None

    def _extract_name(self, soup: BeautifulSoup) -> str | None:
        for selector in self.NAME_SELECTORS:
            el = soup.select_one(selector)
            if el:
                name = el.get_text(strip=True)
                if name:
                    return name[:200]
        return None

    def _detect_coupon(self, soup: BeautifulSoup) -> str | None:
        coupon_selectors = [
            "#promoPriceBlockMessage_feature_div",
            "#couponBadgeRegularVpc",
            ".couponBadge",
            "[data-csa-c-coupon]",
            "#vpcButton",
        ]
        for sel in coupon_selectors:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if text:
                    logger.debug("Amazon coupon detected: %s", text[:80])
                    return text[:100]
        return None

    def _detect_seller(self, soup: BeautifulSoup) -> tuple[str, bool]:
        """Detect the seller name and whether it is sold by Amazon."""
        seller_name: str | None = None

        seller_el = soup.select_one("#sellerProfileTriggerId")
        if seller_el:
            seller_name = seller_el.get_text(strip=True)

        if not seller_name:
            merchant_el = soup.select_one("#merchant-info")
            if merchant_el:
                text = merchant_el.get_text(strip=True)
                match = re.search(r"[Vv]enduto da\s+(.+?)(?:\s+e\s+spedito|\.|$)", text)
                seller_name = match.group(1).strip() if match else text[:100]

        if not seller_name:
            return ("", False)

        amazon_domains = (
            "Amazon.it",
            "Amazon.de",
            "Amazon.fr",
            "Amazon.es",
            "Amazon.co.uk",
            "Amazon.com",
            "Amazon.nl",
            "Amazon.pl",
            "Amazon.se",
            "Amazon.co.jp",
            "Amazon.com.br",
            "Amazon.ca",
            "Amazon.com.au",
            "Amazon EU",
        )
        is_amazon = any(ad.lower() in seller_name.lower() for ad in amazon_domains)
        return (seller_name, is_amazon)

    def _detect_condition(self, soup: BeautifulSoup, seller_name: str = "") -> str:
        """Detect product condition: new, used, or renewed."""
        seller_lower = seller_name.lower()

        if "renewed" in seller_lower or "ricondizionato" in seller_lower:
            return "renewed"
        if "seconda mano" in seller_lower or "warehouse" in seller_lower:
            return "used"

        main_buybox_selectors = [
            "#corePrice_desktop",
            "#corePriceDisplay_desktop_feature_div",
            "#corePrice_feature_div",
            "#apex_desktop",
        ]
        price_sub_selectors = [
            ".priceToPay .a-offscreen",
            ".apexPriceToPay .a-offscreen",
            ".a-price .a-offscreen",
        ]
        for container_sel in main_buybox_selectors:
            container = soup.select_one(container_sel)
            if not container:
                continue
            for price_sel in price_sub_selectors:
                price_el = container.select_one(price_sel)
                if price_el and price_el.get_text(strip=True):
                    logger.debug("Condition=new: found price in %s > %s", container_sel, price_sel)
                    return "new"

        has_used_buybox = bool(
            soup.select_one("#usedOnlyBuybox") or soup.select_one("#used_buybox_desktop")
        )
        if has_used_buybox:
            return "used"

        return "new"

    def _extract_new_price(self, soup: BeautifulSoup) -> Decimal | None:
        """Extract the "new" price when the buybox shows a used/renewed item."""
        for sel in ["[id*=newAccordionRow]", "#newAccordionRow", "#buyBoxAccordion"]:
            for row in soup.select(sel):
                price_el = row.select_one(".a-price .a-offscreen")
                if price_el:
                    parsed = parse_price(price_el.get_text(strip=True))
                    if parsed:
                        return parsed

        nbp = soup.select_one("#newBuyBoxPrice")
        if nbp:
            parsed = parse_price(nbp.get_text(strip=True))
            if parsed:
                return parsed

        new_link = soup.select_one('a[href*="condition=new"]')
        if new_link:
            parsed = parse_price(new_link.get_text(strip=True))
            if parsed:
                return parsed

        return None

    def _try_json_ld_price(self, soup: BeautifulSoup) -> Decimal | None:
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "Product":
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    offer_type = offers.get("@type", "")
                    if offer_type == "AggregateOffer":
                        logger.debug("Skipping AggregateOffer.lowPrice (unreliable on Amazon)")
                        continue
                    price = offers.get("price")
                    if price:
                        return parse_price(str(price))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        return None
