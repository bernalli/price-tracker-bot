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
from price_tracker.core.identity import normalize_url
from price_tracker.core.image_url import normalize_image_url
from price_tracker.core.page_image import (
    ImageDocument,
    ImagePageHTML,
    PriceImageBinding,
    extract_page_image,
)
from price_tracker.core.retry_policy import RetryConfig, with_retry
from price_tracker.core.scraper_base import (
    USER_AGENTS,
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_listing_gone,
    get_headers,
    parse_price,
    retry_after_header,
)
from price_tracker.scrapers.evidence import offer_evidence, offer_urls_match, price_currency

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


# Lower-case phrases Amazon prints when a listing is sold out, per locale.
_UNAVAILABLE_PHRASES: tuple[str, ...] = (
    "non disponibile",  # it
    "currently unavailable",  # com, co.uk, ca, com.au
    "temporarily out of stock",
    "nicht verfügbar",  # de
    "indisponible",  # fr
    "no disponible",  # es
    "niet beschikbaar",  # nl
    "niet leverbaar",
    "niedostępny",  # pl
    "inte tillgänglig",  # se
    "在庫切れ",  # co.jp
    "現在お取り扱いできません",
)

# Containers whose text states the listing's own availability.
_AVAILABILITY_SELECTORS: tuple[str, ...] = (
    "#availability",
    "#outOfStock",
    "#availability_feature_div",
)


def _is_unavailable(
    soup: BeautifulSoup | Tag,
    target_asin: str | None = None,
    *,
    ignore_offer_rows: bool = False,
) -> bool:
    """Whether the page states the product itself is sold out / unavailable."""
    for selector in _AVAILABILITY_SELECTORS:
        for el in soup.select(selector):
            if not _offer_evidence_owned(el, target_asin):
                continue
            if ignore_offer_rows and any(
                "accordionrow" in str(node.get("id") or "").lower() for node in el.parents
            ):
                continue
            text = el.get_text(" ", strip=True).lower()
            if any(phrase in text for phrase in _UNAVAILABLE_PHRASES):
                return True
    return False


# Lower-case phrases stating the offer can be bought now, per locale. They are
# read only after the sold-out phrases: "nicht auf lager" contains "auf lager".
# A bare "available" word is left out on purpose: "available from these
# sellers" points at other offers. Where the wording is not listed here, the
# purchase control of the offer is the evidence.
_AVAILABLE_PHRASES: tuple[str, ...] = (
    "in stock",  # com, co.uk, ca, com.au ("only 3 left in stock")
    "disponibilità immediata",  # it
    "disponibilità: solo",
    "auf lager",  # de
    "en stock",  # fr, es
    "op voorraad",  # nl
    "i lager",  # se
    "在庫あり",  # co.jp
)

# Sold-out wording that contains one of the phrases above or is absent from
# ``_UNAVAILABLE_PHRASES``; it only ever vetoes a positive reading.
_NOT_AVAILABLE_VETOES: tuple[str, ...] = (
    *_UNAVAILABLE_PHRASES,
    "out of stock",
    "nicht auf lager",
    "esaurito",
    "épuisé",
    "rupture de stock",
    "agotado",
    "uitverkocht",
    "ej i lager",
)

# Controls that put this offer in the basket; their ids do not depend on the locale.
_PURCHASE_CONTROLS = (
    "#add-to-cart-button, #buy-now-button, "
    'input[name="submit.add-to-cart"], input[name="submit.buy-now"]'
)

# The buy box lists several offers of one product (new, used, renewed) as rows.
_OFFER_ROW_SELECTOR = '[id*="AccordionRow"]'
_BUYBOX_IDS = frozenset({"desktop_buybox", "buyBoxInner", "buybox", "buybox_feature_div"})
_BUYBOX_SELECTOR = ", ".join(f"#{value}" for value in sorted(_BUYBOX_IDS))


def _offer_scope(price_node: Tag, soup: BeautifulSoup, *, contained: bool = False) -> Tag | None:
    """The markup describing the offer that ``price_node`` prices, or None.

    With several offers on the page it is the row holding the price: a stock
    statement of the used offer says nothing about the new one. A price outside
    every row cannot be tied to one of them. A page with a single offer keeps
    the price and the stock statement in different columns, so its buy box is
    the evidence scope. An unknown layout cannot prove stock from page-wide controls.
    ``contained`` is for a price read outside the main price block (the new
    offer next to a used buy box): only a scope holding the node describes it.
    """
    for node in price_node.parents:
        if "accordionrow" in str(node.get("id") or "").lower():
            return node
    if soup.select_one(_OFFER_ROW_SELECTOR) is not None:
        return None
    # Known buy-box ids nest (``desktop_buybox`` > ``buybox`` > ``buyBoxInner``):
    # the outermost one is the offer's box, as for a price outside every box.
    enclosing = [node for node in price_node.parents if node.get("id") in _BUYBOX_IDS]
    if enclosing:
        return enclosing[-1]
    if contained:
        return None
    # Price and stock can occupy separate columns, but several disjoint buy
    # boxes do not identify which offer the outside price belongs to.
    boxes = [
        box
        for box in soup.select(_BUYBOX_SELECTOR)
        if not any(parent.get("id") in _BUYBOX_IDS for parent in box.parents)
    ]
    return boxes[0] if len(boxes) == 1 else None


def _sold_out_scope(
    price_node: Tag, soup: BeautifulSoup, *, contained: bool = False
) -> BeautifulSoup | Tag | None:
    """Where a sold-out statement binds to the offer that ``price_node`` prices.

    It is the offer's own scope when the layout identifies one. A page with no
    offer rows and no known buy box shows one offer in a layout this module
    does not know: its owned statements still describe that offer, so the page
    is the scope. Several offers that cannot be tied to the price stay unbound.
    """
    scope = _offer_scope(price_node, soup, contained=contained)
    if scope is not None or contained:
        return scope
    if soup.select_one(_OFFER_ROW_SELECTOR) is None and soup.select_one(_BUYBOX_SELECTOR) is None:
        return soup
    return None


def _offer_evidence_owned(element: Tag, target_asin: str | None) -> bool:
    """Exclude other-seller widgets even when they carry no product owner."""
    if not _owned_by(element, target_asin):
        return False
    return not any(
        marker in str(node.get("id") or "").lower()
        for node in (element, *element.parents)
        for marker in ("aod-", "olp-", "other-seller")
    )


def _purchase_control_enabled(element: Tag) -> bool:
    """A disabled control, including one in a disabled fieldset, cannot buy stock."""
    if str(element.get("type") or "").lower() == "hidden":
        return False
    return not any(
        node.has_attr("disabled")
        or str(node.get("aria-disabled") or "").lower() == "true"
        or not {"a-button-disabled", "aok-hidden"}.isdisjoint(node.get("class") or [])
        for node in (element, *element.parents)
    )


def _offer_in_stock(
    price_node: Tag, soup: BeautifulSoup, target_asin: str | None, *, contained: bool = False
) -> bool:
    """Whether the offer of the selected price explicitly states it can be bought.

    Evidence is a stock statement or a purchase control inside the offer's own
    scope and owned by the tracked product. Any sold-out wording in that scope
    wins, so this never turns a doubtful page into a positive reading.
    """
    scope = _offer_scope(price_node, soup, contained=contained)
    if scope is None:
        return False
    statements = [
        el.get_text(" ", strip=True).lower()
        for selector in _AVAILABILITY_SELECTORS
        for el in scope.select(selector)
        if _offer_evidence_owned(el, target_asin)
    ]
    if any(veto in text for text in statements for veto in _NOT_AVAILABLE_VETOES):
        return False
    statements = [text for text in statements if text]
    if statements:
        literals = "|".join(
            re.escape(phrase) for phrase in _AVAILABLE_PHRASES if phrase != "disponibilità: solo"
        )
        affirmative = (
            rf"(?:{literals}"
            r"|only [1-9][0-9]* left in stock(?:\s*[-–—]\s*order soon)?"
            r"|disponibilità: solo [1-9][0-9]*)[.!]?"
        )
        # Unknown, negated, future or conflicting text is not current stock.
        # A purchase control cannot override an unresolved stock statement.
        return all(
            re.fullmatch(affirmative, " ".join(text.split())) is not None for text in statements
        )
    return any(
        _offer_evidence_owned(el, target_asin) and _purchase_control_enabled(el)
        for el in scope.select(_PURCHASE_CONTROLS)
    )


def _has_no_buybox_offer(soup: BeautifulSoup) -> bool:
    """Whether the page has no buy box, only a link to other sellers' offers."""
    return (
        soup.select_one("#buybox-see-all-buying-choices, #buybox-see-all-buying-choices-announce")
        is not None
        and soup.select_one("#add-to-cart-button, #buy-now-button") is None
    )


# Ref tags on the links of Amazon's not-found ("dogs") page; identical in every
# locale, unlike the page's title and copy.
_NOT_FOUND_REF_RE = re.compile(r"ref=cs_404_", re.IGNORECASE)


def _is_not_found_page(soup: BeautifulSoup) -> bool:
    """Whether the page is Amazon's not-found page rather than a product page."""
    if soup.select_one("#productTitle, #title, #dp, #dp-container") is not None:
        return False
    return any(
        _NOT_FOUND_REF_RE.search(str(a.get("href") or "")) for a in soup.find_all("a", href=True)
    )


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
    detect_block_event(
        status_code=response.status_code,
        body=response.text,
        url=url,
        retry_after=retry_after_header(response),
    )
    detect_listing_gone(status_code=response.status_code, url=url)
    response.raise_for_status()
    return ImagePageHTML(response.text, str(response.url))


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
            detect_block_event(
                status_code=response.status_code,
                body=response.text,
                url=url,
                retry_after=retry_after_header(response),
            )
            detect_listing_gone(status_code=response.status_code, url=url)
            response.raise_for_status()
            return ImagePageHTML(response.text, str(response.url))
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
            return ProductInfo(error="Impossibile caricare la pagina Amazon", stock_evidence=False)

        soup = BeautifulSoup(html, "lxml")
        info = ProductInfo(stock_evidence=False)

        requested_asin = _extract_asin(url)
        if requested_asin:
            final_url = getattr(html, "final_url", url)
            final_asin = _extract_asin(final_url)
            page_urls = [
                str(t.get("href") or t.get("content") or "")
                for t in soup.select('link[rel="canonical"], meta[property="og:url"]')
            ]
            if final_asin != requested_asin or any(
                _extract_asin(value) != requested_asin for value in page_urls
            ):
                return ProductInfo(error="Identità Amazon non corrispondente", stock_evidence=False)
            echoes: list[str] = []
            echoes.extend(
                str(t.get("value") or "").strip().upper()
                for t in soup.select('input#ASIN, input[name="ASIN"]')
            )
            if (final_asin is not None and final_asin != requested_asin) or any(
                value and value != requested_asin for value in echoes
            ):
                return ProductInfo(error="Identità Amazon non corrispondente", stock_evidence=False)

        # Check for captcha page — raise so the scheduler quarantines the domain
        # instead of recording a generic "price not found" error.
        if soup.find("form", action=re.compile(r"validateCaptcha")):
            raise CaptchaDetected(marker="amazon-validateCaptcha", url=url)

        # A removed ASIN can come back as Amazon's "dogs" not-found page with a
        # 2xx status: that is a gone listing, not a page we failed to read.
        if _is_not_found_page(soup):
            raise ListingGone(status=404, url=url, http_status_observed=False)

        # Extract price — CSS selectors first (buybox = real price for Amazon).
        # Prices are accepted only if they belong to this product's ASIN, so a
        # recommendation card can never stand in for a missing buy-box price.
        target_asin = _extract_asin(url) or _page_asin(soup)
        document = ImageDocument.parse(soup)
        css_sources: list[Tag] = []
        json_sources: list[str] = []
        json_currencies: list[str | None] = []
        json_currency_errors: list[bool] = []
        json_availability: list[bool | None] = []
        css_price = self._extract_price(soup, target_asin=target_asin, image_sources=css_sources)
        ld_price = self._try_json_ld_price(
            soup,
            image_document=document,
            image_sources=json_sources,
            target_asin=target_asin,
            currencies=json_currencies,
            currency_errors=json_currency_errors,
            availability=json_availability,
        )
        json_selected = css_price is None

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
                json_selected = True
            else:
                info.price = css_price
        else:
            info.price = css_price if css_price is not None else ld_price

        # Extract name
        info.name = self._extract_name(soup)

        # Log coupon presence for debugging
        coupon = self._detect_coupon(soup)
        if coupon:
            logger.info("Amazon coupon detected for %s: %s", url[:60], coupon)

        # Detect seller and condition
        seller_name, _is_sold_by_amazon = self._detect_seller(soup)
        info.seller = seller_name if seller_name else None
        info.condition = self._detect_condition(soup, seller_name=seller_name)

        # If buybox shows used/renewed, try to find the new price and override
        new_override = False
        if info.condition != "new":
            new_sources: list[Tag] = []
            new_price = self._extract_new_price(
                soup, target_asin=target_asin, image_sources=new_sources
            )
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
                json_selected = False
                css_sources = new_sources
                new_override = True

        # Bind negative markup to the selected CSS offer too. A used offer or
        # a foreign recommendation cannot declare the selected new one sold out.
        negative_scope = (
            _sold_out_scope(css_sources[0], soup, contained=new_override)
            if css_sources and not json_selected
            else soup
        )
        if negative_scope is not None and _is_unavailable(
            negative_scope,
            target_asin,
            ignore_offer_rows=json_selected and info.price is not None,
        ):
            info.available = False
            info.stock_evidence = True

        ambiguous = False
        if json_selected:
            info.currency = json_currencies[0] if json_currencies else None
            ambiguous = bool(json_currency_errors and json_currency_errors[0])
            if json_availability and isinstance(json_availability[0], bool):
                if not info.stock_evidence or json_availability[0] is False:
                    info.available = json_availability[0]
                info.stock_evidence = True
        elif css_sources:
            source = css_sources[0]
            price_text = source.get_text(" ", strip=True)
            if "a-price-whole" in (source.get("class") or []):
                symbol = source.parent.find(class_="a-price-symbol") if source.parent else None
                price_text = f"{symbol.get_text(strip=True) if symbol else ''}{info.price}"
            declared = json_currencies[0] if ld_price == info.price and json_currencies else None
            info.currency, ambiguous = price_currency(price_text, declared)
            # A price read from the markup carries no availability of its own.
            # Without this the result stays "availability unknown" forever on
            # the ordinary in-stock page, whose JSON-LD has no offer.
            if not info.stock_evidence and _offer_in_stock(
                source, soup, target_asin, contained=new_override
            ):
                info.available = True
                info.stock_evidence = True
        if ambiguous:
            info.price = None
            info.error = "currency_ambiguous"
            return info

        if info.price is None:
            # No buy box at all, only "see all buying choices" from other sellers:
            # Amazon itself is not selling the product, which is a sold-out state
            # to wait out (restock alert), not a page we failed to read.
            if _has_no_buybox_offer(soup):
                info.available = False
            info.error = "Prezzo non trovato (prodotto non disponibile?)"

        if info.price is not None:
            info.image_url = self._image_for_price(
                soup,
                url,
                getattr(html, "final_url", url),
                document,
                json_sources if json_selected else None,
                css_sources,
            )
        return info

    @staticmethod
    def _image_for_price(
        soup: BeautifulSoup,
        url: str,
        final_url: str,
        document: ImageDocument,
        json_sources: list[str] | None,
        css_sources: list[Tag],
    ) -> str | None:
        asin = _extract_asin(url)
        if asin is None or _extract_asin(final_url) != asin:
            return None
        echoes = [
            _extract_asin(str(tag.get("href") or tag.get("content") or ""))
            for tag in soup.select('link[rel="canonical"], meta[property="og:url"]')
        ]
        echoes.extend(
            str(tag.get("value") or "").upper()
            for tag in soup.select('input#ASIN, input[name="ASIN"]')
        )
        if not echoes or any(echo != asin for echo in echoes):
            return None

        def owned(element: Tag) -> bool:
            for node in (element, *element.parents):
                owner = node.get("data-asin")
                if owner is not None and str(owner).strip().upper() != asin:
                    return False
                context = " ".join(
                    (
                        str(node.get("id") or ""),
                        " ".join(node.get("class") or []),
                        str(node.get("data-feature-name") or ""),
                    )
                ).lower()
                if any(
                    marker in context
                    for marker in (
                        "recommend",
                        "carousel",
                        "sims",
                        "similar",
                        "sponsored",
                        "sp_detail",
                        "bundle",
                    )
                ):
                    return False
            return True

        if json_sources is not None:
            if not json_sources:
                return None
            return extract_page_image(
                soup,
                page_url=final_url,
                price_binding=PriceImageBinding(
                    document,
                    url,
                    tuple(json_sources),
                    normalize_url(url),
                    True,
                ),
            )
        if not css_sources or not all(owned(node) for node in css_sources):
            return None
        images = set()
        for node in soup.select("#landingImage"):
            if owned(node):
                for attribute in ("data-old-hires", "src"):
                    image = normalize_image_url(node.get(attribute), base_url=final_url)
                    if image is not None:
                        images.add(image)
                        break
        if len(images) > 1:
            return None
        if images:
            return images.pop()
        return extract_page_image(
            soup,
            page_url=final_url,
            price_binding=PriceImageBinding(
                document,
                url,
                (),
                normalize_url(url),
                True,
                price_source="trusted",
                page_scope="product",
            ),
        )

    def _extract_price(
        self,
        soup: BeautifulSoup,
        *,
        target_asin: str | None = None,
        image_sources: list[Tag] | None = None,
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
                        if image_sources is not None:
                            image_sources.append(el)
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
                    if image_sources is not None:
                        image_sources.append(el)
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
        self,
        soup: BeautifulSoup,
        *,
        target_asin: str | None = None,
        image_sources: list[Tag] | None = None,
    ) -> Decimal | None:
        """Extract the "new" price when the buybox shows a used/renewed item."""
        for sel in ["[id*=newAccordionRow]", "#newAccordionRow", "#buyBoxAccordion"]:
            for row in soup.select(sel):
                price_el = row.select_one(".a-price .a-offscreen")
                if price_el and _owned_by(price_el, target_asin):
                    parsed = parse_price(price_el.get_text(strip=True))
                    if parsed:
                        if image_sources is not None:
                            image_sources.append(price_el)
                        return parsed

        nbp = soup.select_one("#newBuyBoxPrice")
        if nbp and _owned_by(nbp, target_asin):
            parsed = parse_price(nbp.get_text(strip=True))
            if parsed:
                if image_sources is not None:
                    image_sources.append(nbp)
                return parsed

        new_link = soup.select_one('a[href*="condition=new"]')
        if new_link and _owned_by(new_link, target_asin):
            parsed = parse_price(new_link.get_text(strip=True))
            if parsed:
                if image_sources is not None:
                    image_sources.append(new_link)
                return parsed

        return None

    def _try_json_ld_price(
        self,
        soup: BeautifulSoup,
        *,
        image_document: ImageDocument | None = None,
        image_sources: list[str] | None = None,
        target_asin: str | None = None,
        currencies: list[str | None] | None = None,
        currency_errors: list[bool] | None = None,
        availability: list[bool | None] | None = None,
    ) -> Decimal | None:
        document = image_document or ImageDocument.parse(soup)
        choices = []
        if not document.valid:
            return None
        for ref, data in document.nodes.items():
            kinds = data.get("@type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if "Product" not in kinds:
                continue
            if target_asin and not self._jsonld_owned_by(data, target_asin):
                continue
            # Keep the established JSON numeric representation for pricing;
            # strict document parsing remains the admission/provenance boundary.
            parts = ref.split("/")
            raw = json.loads(document.payloads[int(parts[0].split(":")[1])])
            for part in parts[1:]:
                key = part.replace("~1", "/").replace("~0", "~")
                raw = raw[int(key)] if isinstance(raw, list) else raw[key]
            if target_asin and not offer_urls_match(
                raw.get("offers", {}),
                lambda value: value.startswith("#") or _extract_asin(value) == target_asin,
            ):
                return None
            if target_asin:
                offers = raw.get("offers", {})
                for offer in offers if isinstance(offers, list) else [offers]:
                    if not isinstance(offer, dict):
                        continue
                    try:
                        identity = {
                            key: offer[key] for key in ("asin", "sku", "productID") if key in offer
                        }
                        if identity and not self._jsonld_owned_by(
                            {"asin": target_asin, **identity}, target_asin
                        ):
                            return None
                        items = offer.get("itemOffered", [])
                        for item in items if isinstance(items, list) else [items]:
                            if (
                                isinstance(item, str)
                                and (_extract_asin(item) or target_asin) != target_asin
                            ):
                                return None
                            if isinstance(item, dict) and not self._jsonld_owned_by(
                                {"asin": target_asin, **item}, target_asin
                            ):
                                return None
                    except ValueError:
                        return None
            selected = offer_evidence(raw.get("offers", {}))
            if selected is not None:
                choices.append((ref, selected))
        if not choices:
            return None
        selected = choices[0][1]
        if any(
            (item["price"], item["currency"], item["available"], item["currency_ambiguous"])
            != (
                selected["price"],
                selected["currency"],
                selected["available"],
                selected["currency_ambiguous"],
            )
            for _, item in choices[1:]
        ):
            return None
        if currencies is not None:
            currencies.append(selected["currency"])
        if currency_errors is not None:
            currency_errors.append(selected["currency_ambiguous"])
        if availability is not None:
            availability.append(selected["available"])
        if image_sources is not None and image_document is not None:
            image_sources.extend(ref for ref, _ in choices)
        return selected["price"]

    @staticmethod
    def _jsonld_owned_by(data: dict, target_asin: str) -> bool:
        identifiers: list[str] = []
        for key in ("url", "@id", "sku", "asin", "productID"):
            if key not in data:
                continue
            values = data[key] if isinstance(data[key], list) else [data[key]]
            if not values:
                return False
            for value in values:
                if not isinstance(value, str):
                    return False
                asin = _extract_asin(value)
                match = re.fullmatch(r"(?:asin[:\s]+)?([A-Z0-9]{10})", value.strip(), re.IGNORECASE)
                if asin is None and match:
                    asin = match.group(1).upper()
                if asin:
                    identifiers.append(asin)
                elif key in {"url", "asin"} or (key == "@id" and not value.startswith("#")):
                    return False
        return bool(identifiers) and all(asin == target_asin for asin in identifiers)
