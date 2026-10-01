"""Amazon-specific scraper.

Amazon has aggressive anti-bot measures — this scraper uses targeted
selectors, careful header management, and a fresh validated HTTP client for
suspiciously small responses. Independent network backends are disabled.
"""

from __future__ import annotations

import json
import logging
import random
import re
from decimal import Decimal
from typing import ClassVar
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from price_tracker.core.exceptions import BlockEvent, CaptchaDetected, ListingGone
from price_tracker.core.http_client import build_client, public_request
from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    USER_AGENTS,
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_currency,
    detect_listing_gone,
    get_headers,
    parse_price,
    select_jsonld_offer,
)

logger = logging.getLogger(__name__)

# Path shapes that name a single product by its ASIN (ten letters or digits).
_ASIN_PATH_RE = re.compile(
    r"/(?:dp/product|dp|gp/product|gp/aw/d|gp/offer-listing|exec/obidos/ASIN|o/ASIN)"
    r"/([A-Z0-9]{10})(?=[/?]|$)",
    re.IGNORECASE,
)


def _extract_asin(url: str) -> str | None:
    """Return the ASIN named by a product URL's path, or None."""
    match = _ASIN_PATH_RE.search(urlparse(url).path)
    return match.group(1).upper() if match else None


def _page_asin(soup: BeautifulSoup) -> str | None:
    """Return the page's own ASIN from its canonical link or hidden ASIN input."""
    canonical = soup.find("link", rel="canonical")
    if isinstance(canonical, Tag):
        asin = _extract_asin(str(canonical.get("href") or ""))
        if asin:
            return asin
    field = soup.select_one("input#ASIN, input[name='ASIN']")
    if field is not None:
        value = str(field.get("value") or "").strip().upper()
        if re.fullmatch(r"[A-Z0-9]{10}", value):
            return value
    return None


def _owned_by(element: Tag, target_asin: str | None) -> bool:
    """Whether a price node may belong to the tracked product.

    Recommendation, sponsored and carousel cards carry the ``data-asin`` of the
    product they advertise, with price markup identical to the main price. The
    nearest ancestor carrying ``data-asin`` decides: it must name the tracked
    ASIN, so an empty value or an unknown tracked ASIN rejects the node. A node
    with no such ancestor belongs to the page itself and is accepted.
    """
    for node in (element, *element.parents):
        owner = node.get("data-asin")
        if owner is None:
            continue
        return target_asin is not None and str(owner).strip().upper() == target_asin
    return True


@with_retry(RetryConfig(max_attempts=3, base_wait=2.0, max_wait=10.0))
async def _fetch_amazon_html(
    url: str, client: httpx.AsyncClient, extra_headers: dict[str, str] | None = None
) -> str:
    """Single GET attempt with browser-like headers. Tenacity handles retries."""
    headers = get_headers(extra_headers)
    response = await public_request(client, "GET", url, headers=headers)
    # Surface 403/429/WAF/CAPTCHA as a BlockEvent and 404/410 as ListingGone
    # BEFORE raise_for_status — same schema as shopify.py (#16), so neither
    # collapses into a generic httpx.HTTPStatusError further down the chain.
    detect_block_event(status_code=response.status_code, body=response.text, url=url)
    detect_listing_gone(status_code=response.status_code, url=url)
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
        async with build_client(timeout=30.0) as fresh:
            response = await public_request(fresh, "GET", url, headers=headers)
            # Same schema as the primary fetch: block/gone detection precedes
            # raise_for_status on this path too (#16).
            detect_block_event(status_code=response.status_code, body=response.text, url=url)
            detect_listing_gone(status_code=response.status_code, url=url)
            response.raise_for_status()
            return response.text
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("Fresh client retry failed: %s", e)
        return None


async def _fetch_via_curl_cffi(url: str) -> str | None:
    """Disabled: this backend cannot bind connections to validated addresses."""
    return None


async def _fetch_via_scrapling(url: str) -> str | None:
    """Disabled: this backend cannot bind connections to validated addresses."""
    return None


async def _fetch_amazon_page(
    url: str, client: httpx.AsyncClient, extra_headers: dict[str, str] | None = None
) -> str | None:
    """Fetch Amazon page with retry + fallback chain.

    Returns the page HTML, or None on a non-block, non-gone failure (e.g. a
    5xx or a network error). On a hard block (403/429/WAF/CAPTCHA) that
    survives every fallback, re-raises the :class:`BlockEvent` subclass the
    primary fetch produced, so the scheduler quarantines the domain instead of
    recording a generic error. On 404/410 raises :class:`ListingGone`
    immediately: the listing is gone, not blocked, and no fallback fetch
    changes that, so none is attempted.
    """
    html: str | None = None
    block_exc: BlockEvent | None = None
    try:
        html = await _fetch_amazon_html(url, client, extra_headers)
    except BlockEvent as e:
        block_exc = e
        logger.warning("Blocked (%s) for %s", e, url[:60])
    except httpx.HTTPStatusError as e:
        logger.warning("HTTP %s for %s after retries", e.response.status_code, url[:60])
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Fetch error for %s: %s", url[:60], e)

    # Fresh-client retry on suspiciously small responses. This call is
    # enrichment only: we already have SOME html from the primary fetch, so a
    # block/gone on THIS retry says nothing about the page already in hand —
    # it must never discard it (same principle as shopify.py's HTML-for-
    # currency-only fetch swallowing BlockEvent/ListingGone).
    if html and len(html) < 80000 and "application/ld+json" not in html:
        logger.info("Amazon response is small (%d chars), retrying with fresh client", len(html))
        try:
            fresh_html = await _fetch_with_fresh_client(url)
        except (BlockEvent, ListingGone) as e:
            logger.debug("Fresh client retry blocked/gone (%s), keeping primary html", e)
            fresh_html = None
        if fresh_html and len(fresh_html) > len(html):
            logger.info("Fresh client got %d chars (vs %d)", len(fresh_html), len(html))
            html = fresh_html

    if html:
        return html

    if block_exc is not None:
        html = await _fetch_via_curl_cffi(url)
        if html:
            return html
        html = await _fetch_via_scrapling(url)
        if html:
            return html
        # Hard block survived every fallback → signal quarantine to the scheduler.
        raise block_exc

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
                resp = await public_request(
                    client,
                    "HEAD",
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
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

        # Check for captcha page — raise so the scheduler quarantines the domain
        # instead of recording a generic "price not found" error.
        if soup.find("form", action=re.compile(r"validateCaptcha")):
            raise CaptchaDetected(marker="amazon-validateCaptcha", url=url)

        # Check for "currently unavailable"
        unavail = soup.find(id="availability")
        if unavail and "non disponibile" in unavail.get_text().lower():
            info.available = False

        # Extract price — CSS selectors first (buybox = real price for Amazon).
        # Prices are accepted only if they belong to this product's ASIN, so a
        # recommendation card can never stand in for a missing buy-box price.
        target_asin = _extract_asin(url) or _page_asin(soup)
        css_price = self._extract_price(soup, target_asin=target_asin)
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
            new_price = self._extract_new_price(soup, target_asin=target_asin)
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

    def _extract_price(
        self, soup: BeautifulSoup, *, target_asin: str | None = None
    ) -> Decimal | None:
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
                    if not _owned_by(el, target_asin):
                        continue
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
                if not _owned_by(el, target_asin):
                    continue
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

        # Amazon shows used and Warehouse offers in the *same* core container as
        # new ones, so "there is a price here" proves nothing about condition.
        # Read the container's own words before falling back to that heuristic,
        # otherwise a second-hand buy box is reported as a new-product price.
        for container_sel in main_buybox_selectors:
            container = soup.select_one(container_sel)
            if container is None:
                continue
            box_text = container.get_text(" ", strip=True).lower()
            if "ricondizionat" in box_text or "renewed" in box_text:
                return "renewed"
            if "usato" in box_text or "seconda mano" in box_text or "used" in box_text:
                return "used"

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

    def _extract_new_price(
        self, soup: BeautifulSoup, *, target_asin: str | None = None
    ) -> Decimal | None:
        """Extract the "new" price when the buybox shows a used/renewed item."""
        for sel in ["[id*=newAccordionRow]", "#newAccordionRow", "#buyBoxAccordion"]:
            for row in soup.select(sel):
                price_el = row.select_one(".a-price .a-offscreen")
                if price_el and _owned_by(price_el, target_asin):
                    parsed = parse_price(price_el.get_text(strip=True))
                    if parsed:
                        return parsed

        nbp = soup.select_one("#newBuyBoxPrice")
        if nbp and _owned_by(nbp, target_asin):
            parsed = parse_price(nbp.get_text(strip=True))
            if parsed:
                return parsed

        new_link = soup.select_one('a[href*="condition=new"]')
        if new_link and _owned_by(new_link, target_asin):
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
                    # Shared offer selection: skips financing entries and
                    # AggregateOffer.lowPrice, returns the representative
                    # concrete price — never offers[0] blindly (#54).
                    selected = select_jsonld_offer(data.get("offers", {}))
                    if selected is not None:
                        return selected[0]
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        return None
