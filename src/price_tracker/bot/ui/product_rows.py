"""Single-line product summaries for narrow Telegram clients."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape
from typing import TYPE_CHECKING, Any

from price_tracker.bot.messages import _, current_locale
from price_tracker.bot.ui.width import display_width, sanitize_label, truncate_to_width
from price_tracker.i18n.format import change, money

if TYPE_CHECKING:
    from collections.abc import Mapping

ROW_WIDTH = 32


def amount(value: object) -> Decimal | None:
    """Accept stored or queued amounts without trusting malformed payloads."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and 0 <= parsed < Decimal(10) ** 15 else None


def product_row(
    name: str,
    current: Decimal | None,
    *,
    initial: Decimal | None = None,
    currency: str = "EUR",
    mark: str = "",
    html: bool = True,
) -> str:
    """Put price/change first; allocate only the remaining display cells to the name.

    Buttons use the same layout as literal text. IDs belong in callbacks and
    list pages. Compact scientific numbers keep extreme amounts/changes
    readable without truncating a monetary value.
    """
    current, initial = amount(current), amount(initial)
    loc = current_locale()
    try:
        price = money(current, currency, locale=loc) if current is not None else _("N/A")
    except ValueError:
        currency = ""
        price = f"{current:.2f}" if current is not None else _("N/A")
    try:
        delta = change(initial, current, locale=loc) if initial and current is not None else ""
    except ValueError:
        delta = _compact_change(initial, current) if initial and current is not None else ""
    delta = delta.replace("▼ ", "▼").replace("▲ ", "▲")
    status = f" {sanitize_label(mark)}" if mark else ""
    extra = f" {delta}" if delta else ""
    if display_width(price + extra) > ROW_WIDTH - 8:
        if current is not None:
            price = f"{current:.2E} {currency}".rstrip()
        if delta and initial and current is not None:
            delta = _compact_change(initial, current)
            extra = f" {delta}"
    status = truncate_to_width(status, max(0, ROW_WIDTH - display_width(price + extra) - 5))
    prefix = price + extra + status
    budget = max(0, ROW_WIDTH - display_width(prefix) - 1)
    shown = truncate_to_width(sanitize_label(name) or _("Unknown"), budget)
    if html:
        prefix = f"<b>{escape(price)}</b>{escape(extra + status)}"
        shown = escape(shown)
    return f"{prefix} {shown}".rstrip()


def _compact_change(initial: Decimal, current: Decimal) -> str:
    ratio = abs(current - initial) / initial * 100
    return f"{'▼' if current < initial else '▲'}{ratio:.1E}%"


def record_row(product: Mapping[str, Any], *, html: bool = True) -> str:
    """Adapt a stored product to the common row without changing its membership."""
    mark = ""
    if not product.get("is_active", True):
        mark = "⏸"
    elif product.get("consecutive_errors", 0):
        mark = "⚠️"
    elif not product.get("is_available", True):
        mark = "🚫"
    return product_row(
        str(product.get("name") or _("Unknown")),
        amount(product.get("current_price")),
        initial=amount(product.get("initial_price")),
        currency=str(product.get("currency") or "EUR"),
        mark=mark,
        html=html,
    )
