"""Currency and offer evidence kept with the price that selected it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from price_tracker.core import currencies, pricegrammar
from price_tracker.core.money import Money, normalize_currency_code
from price_tracker.core.scraper_base import (
    _is_financing_offer,
    detect_currency,
    jsonld_offer_availability,
    select_jsonld_offer,
)


def price_currency(
    text: object, declared: object = None, *, optional_declaration: bool = False
) -> tuple[str | None, bool]:
    """Return source currency and whether explicit tokens remain unresolved/conflicting."""
    # A declaration is one currency code/token, not another price text. Validate
    # it independently so a visible unit cannot hide malformed source metadata.
    code = normalize_currency_code(declared)
    if code is None and isinstance(declared, str):
        residue, _, _ = pricegrammar._strip_currency_token(declared.strip())
        if not residue:
            code = detect_currency(declared)
    if declared is not None and code is None:
        if not optional_declaration:
            return None, True
        # Shopify's optional API metadata may be absent/unusable; the caller
        # must then obtain a unit from owned page evidence. A meaningful but
        # ambiguous/conflicting token must never be downgraded to absent.
        if isinstance(declared, str):
            _, iso, candidates = pricegrammar._strip_currency_token(declared.strip())
            if iso is not None or candidates is not None:
                return None, True
    visible = detect_currency(text) if isinstance(text, str) else None
    if code and visible and code != visible:
        return None, True
    expected = frozenset({code}) if code else frozenset()
    resolved = currencies.detect_currency(text, expected=expected) or code or visible
    for value in (text, declared):
        if not isinstance(value, str):
            continue
        residue = value.strip()
        for _ in range(2):
            residue, iso, candidates = pricegrammar._strip_currency_token(residue)
            if iso and resolved and iso != resolved:
                return None, True
            if candidates is not None and (resolved is None or resolved not in candidates):
                return None, True
    return resolved, False


def offer_urls_match(offers: object, matches: Callable[[str], bool]) -> bool:
    """An owned product cannot authorize an explicitly contradictory child offer."""
    entries = offers if isinstance(offers, list) else [offers]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("url", "@id"):
            if key not in entry:
                continue
            values = entry[key] if isinstance(entry[key], list) else [entry[key]]
            if not values or not all(isinstance(value, str) and matches(value) for value in values):
                return False
    return True


def offer_evidence(offers: object, *, strict: bool = False) -> dict[str, Any] | None:
    """Return price, currency and stock from the SAME eligible concrete offer.

    ``strict`` uses the modern structured-price eligibility rules, including
    exclusion of reference prices. Default selection retains the legacy numeric
    policy for existing callers. No amount is returned for conflicting currencies.
    An explicitly sold-out unpriced offer remains an observation, not a price.
    """
    entries = offers if isinstance(offers, list) else [offers]
    best: dict[str, Any] | None = None
    codes: set[str] = set()
    eligible: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if strict:
            if pricegrammar._classify_offer(entry) is not None:
                continue
            eligible.append(entry)
            code, ambiguous = price_currency(entry.get("price"), entry.get("priceCurrency"))
            if entry.get("priceCurrency") is not None and code is None:
                ambiguous = True
            if code:
                codes.add(code)
            if ambiguous or len(codes) > 1:
                return {
                    "price": None,
                    "currency": None,
                    "currency_ambiguous": True,
                    "available": None,
                }
            normalized = {**entry, "priceCurrency": code} if code else entry
            strict_selected = pricegrammar.select_offer(normalized)
            if not isinstance(strict_selected, Money):
                continue
            amount = strict_selected.amount
        else:
            if _is_financing_offer(entry):
                continue
            eligible.append(entry)
            selected = select_jsonld_offer(entry)
            if selected is None:
                continue
            amount, _ = selected
        code, ambiguous = price_currency(str(entry.get("price", "")), entry.get("priceCurrency"))
        if code:
            codes.add(code)
        available = jsonld_offer_availability(entry)
        if ambiguous or len(codes) > 1:
            return {"price": None, "currency": None, "currency_ambiguous": True, "available": None}
        if strict and best is not None and code != best["currency"]:
            # A priced offer with no unit cannot be ordered against a known unit
            # or disappear merely because the known amount happens to be larger.
            return {"price": None, "currency": None, "currency_ambiguous": True, "available": None}
        if (
            best is None
            or amount > best["price"]
            or (amount == best["price"] and available is True and best["available"] is not True)
        ):
            best = {
                "price": amount,
                "currency": code,
                "currency_ambiguous": ambiguous,
                "available": available,
            }
    if (
        best is None
        and eligible
        and all(
            isinstance(entry, dict) and jsonld_offer_availability(entry) is False
            for entry in eligible
        )
    ):
        return {
            "price": None,
            "currency": next(iter(codes), None),
            "currency_ambiguous": False,
            "available": False,
        }
    return best
