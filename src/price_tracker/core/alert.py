"""Price alert formatting and threshold trigger logic."""

from __future__ import annotations

import html
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import datetime

ThresholdType = Literal["percentage", "absolute", "target", "any_drop"]


_CURRENCY_SYMBOLS: dict[str, str] = {
    "EUR": "€",
    "USD": "$",
    "GBP": "£",
    "CHF": "CHF",
    "JPY": "¥",
    "SEK": "kr",
    "NOK": "kr",
    "DKK": "kr",
    "PLN": "zł",
    "CZK": "Kč",
}


def _currency_symbol(currency: str) -> str:
    return _CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())


def _escape_html(text: str) -> str:
    return html.escape(str(text), quote=True)


@dataclass(frozen=True)
class PriceAlert:
    """All data needed to render a price-drop notification."""

    product_id: int
    product_name: str
    url: str
    old_price: Decimal
    new_price: Decimal
    currency: str
    threshold_type: ThresholdType
    threshold_value: Decimal


def crosses_threshold(
    *,
    old: Decimal,
    new: Decimal,
    threshold_type: ThresholdType,
    threshold_value: Decimal,
) -> bool:
    """Return True if the price drop from `old` to `new` triggers a notification."""
    if new >= old:
        return False
    drop = old - new

    if threshold_type == "any_drop":
        return True  # sentinel: any decrease (new < old, guaranteed above) triggers
    if threshold_type == "percentage":
        if old == 0:
            return False
        pct = (drop / old) * 100
        return pct >= threshold_value
    if threshold_type == "absolute":
        return drop >= threshold_value
    if threshold_type == "target":
        return new <= threshold_value
    return False


def format_alert(alert: PriceAlert) -> str:
    """Format a price-drop alert as Telegram HTML."""
    sym = _currency_symbol(alert.currency)
    name = _escape_html(alert.product_name)
    url = _escape_html(alert.url)
    old = alert.old_price
    new = alert.new_price
    drop = old - new
    drop_pct = (drop / old * 100) if old > 0 else Decimal("0")

    return (
        f"📉 <b>Price drop!</b>\n\n"
        f"<b>{name}</b>\n"
        f'<a href="{url}">View product</a>\n\n'
        f"Was: <s>{old} {sym}</s>\n"
        f"Now: <b>{new} {sym}</b>\n"
        f"Drop: -{drop} {sym} ({drop_pct:.1f}%)"
    )


def format_error_notification(
    *,
    product: dict[str, str],
    error_count: int,
    max_errors: int,
) -> str:
    """Format an alert for a product that has hit max consecutive errors."""
    name = _escape_html(product.get("name") or product.get("url", "?"))
    return (
        f"⚠️ <b>Tracking suspended</b>\n\n"
        f"<b>{name}</b>\n"
        f"Failed {error_count}/{max_errors} consecutive checks. "
        f"Use /reactivate to retry."
    )


def format_quarantine_notification(
    *,
    domain: str,
    reason: str,
    locked_until: datetime | None,
) -> str:
    """Notify the user that a domain entered anti-bot quarantine (one-shot).

    Sent on the CLOSED → LOCKED transition only, so the user learns a site has
    started failing without being spammed on every individual check.
    """
    until = ""
    if locked_until is not None:
        until = f"\n🔁 Riprovo da solo dopo: {locked_until:%Y-%m-%d %H:%M} UTC"
    return (
        f"🔒 <b>Sito in pausa automatica</b>\n\n"
        f"<b>{_escape_html(domain)}</b> ha fallito troppi controlli "
        f"({_escape_html(reason)}).\n"
        f"Sospendo temporaneamente i check su questo sito per non insistere "
        f"contro un blocco.{until}\n\n"
        f"Dettagli con /errori."
    )
