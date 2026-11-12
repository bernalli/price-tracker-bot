"""Requested-item and local price evidence for the secondary retailers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from decimal import Decimal

    from bs4 import BeautifulSoup

from price_tracker.core.identity import RequestedIdentity, check_echoes
from price_tracker.core.scraper_base import parse_price, unwrap_jsonld_graph
from price_tracker.scrapers.evidence import offer_evidence, offer_urls_match, price_currency


class EvidenceResult(TypedDict, total=False):
    price: Decimal
    currency: str
    name: str
    available: bool
    currency_ambiguous: bool
    identity_mismatch: bool


def page_matches(soup: BeautifulSoup, url: str, final_url: str) -> bool:
    """Every present document echo must agree with the requested identity."""
    try:
        requested = RequestedIdentity.from_url(url)
    except ValueError:
        return False
    for node in soup.select('link[rel="canonical"], meta[property="og:url"]'):
        raw = node.get("href") if node.name == "link" else node.get("content")
        if not isinstance(raw, str) or requested.resolve(raw, final_url) != requested.url:
            return False
    canonical = soup.select_one('link[rel="canonical"]')
    raw_canonical = canonical.get("href") if canonical else None
    return check_echoes(
        requested,
        final_url=final_url,
        canonical=raw_canonical if isinstance(raw_canonical, str) else None,
    ).ok


def product_evidence(soup: BeautifulSoup, url: str) -> EvidenceResult | None:
    """Select an owned Product before interpreting its own concrete offers.

    A single URL-less Product may use the already verified page identity. Multiple
    unidentified products are ambiguous. Explicit contradictory identities never
    authorize a DOM fallback or an unavailable observation.
    """
    requested = RequestedIdentity.from_url(url)
    products: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for item in unwrap_jsonld_graph(payload):
            kinds = item.get("@type", [])
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if any(
                isinstance(kind, str) and kind.rsplit("/", 1)[-1] == "Product" for kind in kinds
            ):
                products.append(item)
    if not products:
        return None
    owned = []
    for item in products:
        references = [item[key] for key in ("url", "@id") if key in item]
        if references:
            values = [
                value for ref in references for value in (ref if isinstance(ref, list) else [ref])
            ]
            if values and all(
                isinstance(value, str) and requested.resolve(value) == requested.url
                for value in values
            ):
                owned.append(item)
        elif len(products) == 1:
            owned.append(item)
    if len(owned) != 1:
        return {"identity_mismatch": True}
    item = owned[0]
    offers = item.get("offers")
    if not offer_urls_match(offers, lambda value: requested.resolve(value) == requested.url):
        return {"identity_mismatch": True}
    result: EvidenceResult = {}
    name = item.get("name")
    if isinstance(name, str) and name:
        result["name"] = name[:200]
    selected = offer_evidence(offers, strict=True)
    if selected is not None:
        if selected["price"] is not None:
            result["price"] = selected["price"]
        if selected["currency"] is not None:
            result["currency"] = selected["currency"]
        if selected["available"] is not None:
            result["available"] = selected["available"]
        if selected["currency_ambiguous"]:
            result["currency_ambiguous"] = True
    return result or None


def dom_evidence(text: str, declared: object = None) -> EvidenceResult | None:
    """Parse the complete visible price without stripping financing qualifiers."""
    text = text.replace("US $", "US$")
    currency, ambiguous = price_currency(text, declared)
    if declared is not None and currency is None:
        ambiguous = True
    if ambiguous:
        return {"currency_ambiguous": True}
    amount = parse_price(text, currency=currency)
    if amount is None:
        return None
    if currency is None:
        return {"price": amount}
    return {"price": amount, "currency": currency}
