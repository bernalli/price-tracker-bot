"""Price alert formatting and threshold trigger logic."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.messages import N_, _
from price_tracker.bot.ui.product_rows import product_row
from price_tracker.core.notices import (
    MAX_LISTED_PRODUCTS,
    NoticeGroup,
    OperationalEvent,
)
from price_tracker.core.textlimits import (
    DOMAIN_BUDGET,
    truncate_visible,
)

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
    # User- and site-controlled values must remain inside one renderer row:
    # split_message uses newline boundaries and must never split an open tag.
    single_line = str(text).replace("\r", " ").replace("\n", " ")
    return html.escape(single_line, quote=True)


_UNREADABLE_COPY = (
    N_("Price unreadable on {domain}"),
    N_(
        "The pages load, but the price could not be read anymore "
        "(layout change or a different offer)."
    ),
    N_("Reactivate to run a fresh check right now."),
    False,
)

_REASON_COPY: dict[str, tuple[str, str, str, bool]] = {
    "listing_gone": (
        N_("Listings removed on {domain}"),
        N_("These pages answer HTTP 404/410: the store took them off the catalog."),
        N_("Deleting keeps your list clean. Reactivate only if the store restores them."),
        True,
    ),
    "parse_error": _UNREADABLE_COPY,
    "price_none": _UNREADABLE_COPY,
    "no_scraper": _UNREADABLE_COPY,
    "condition_mismatch": _UNREADABLE_COPY,
    "implausible_read": _UNREADABLE_COPY,
    "http_error": (
        N_("Site unreachable: {domain}"),
        N_("The site did not answer {max} checks in a row."),
        N_("Try again later. Reactivate once the site is back."),
        False,
    ),
    "unexpected": (
        N_("Site unreachable: {domain}"),
        N_("The site did not answer {max} checks in a row."),
        N_("Try again later. Reactivate once the site is back."),
        False,
    ),
    "block": (
        N_("Blocked by {domain}"),
        N_("The site is refusing automated checks (anti-bot)."),
        N_("Domain quarantine already paces retries. Reactivate once it clears."),
        False,
    ),
}
_DEFAULT_COPY = (
    N_("Tracking suspended on {domain}"),
    N_("Checks kept failing."),
    N_("Reactivate to retry."),
    False,
)
_STATUS_RE = re.compile(r"\b(404|410)\b")


def _copy_for(reason: str) -> tuple[str, str, str, bool]:
    """Return the closed-copy mapping, with a safe default for unknown reasons."""
    return _REASON_COPY.get(reason, _DEFAULT_COPY)


def _why(reason: str | None, detail: str | None) -> str:
    """Render a compact, reason-aware explanation for a product row."""
    if reason == "listing_gone":
        status_match = _STATUS_RE.search(detail or "")
        status = status_match.group(1) if status_match is not None else "404"
        return _("page not found (HTTP {status})").format(status=status)
    if reason == "block":
        return _("blocked")
    if reason in {
        "parse_error",
        "price_none",
        "no_scraper",
        "condition_mismatch",
        "implausible_read",
    }:
        return _("price not readable")
    if reason in {"http_error", "unexpected"}:
        return _("site unreachable")
    return _("check failed")


def _display_timestamp(value: str | None) -> str | None:
    """Return a compact UTC timestamp, or ``None`` when persisted data is invalid."""
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _product_lines(event: OperationalEvent) -> list[str]:
    """Keep each affected product on one narrow row; the heading explains its state."""
    return [
        product_row(
            event.product_name or event.url,
            event.last_price,
            currency=event.currency or "EUR",
            mark="⚠️",
        )
    ]


def format_operational_notice(group: NoticeGroup) -> str:
    """Format a suspended operational group as Telegram HTML under field budgets."""
    if group.event != "suspended":
        raise ValueError("format_operational_notice requires a suspended group")
    if not group.events:
        raise ValueError("notice group must contain at least one event")
    title, headline, hint, _delete_first = _copy_for(group.primary_reason)
    domain = _escape_html(truncate_visible(group.group_key, DOMAIN_BUDGET))
    max_errors = group.events[0].max_errors
    lines = [
        f"⚠️ <b>{_(title).format(domain=domain)}</b> ({len(group.events)})",
        "",
        _(headline).format(max=max_errors),
        "",
    ]
    for event in group.events[:MAX_LISTED_PRODUCTS]:
        lines.extend(_product_lines(event))
    remaining = len(group.events) - MAX_LISTED_PRODUCTS
    if remaining > 0:
        lines.append(_("… and {k} more").format(k=remaining))
    lines.extend(("", _(hint)))
    return "\n".join(lines)


def format_warning_notice(group: NoticeGroup) -> str:
    """Format a pre-suspension warning group as Telegram HTML."""
    if group.event != "warning":
        raise ValueError("format_warning_notice requires a warning group")
    if not group.events:
        raise ValueError("notice group must contain at least one event")
    domain = _escape_html(truncate_visible(group.group_key, DOMAIN_BUDGET))
    first = group.events[0]
    lines = [
        f"⏳ <b>{_('Checks failing on {domain}').format(domain=domain)}</b> ({len(group.events)})",
        "",
        _(
            "{n} products failed {count}/{max} checks in a row. If it keeps failing "
            "they will be suspended automatically."
        ).format(n=len(group.events), count=first.error_count, max=first.max_errors),
        "",
    ]
    for event in group.events[:MAX_LISTED_PRODUCTS]:
        lines.extend(_product_lines(event))
    remaining = len(group.events) - MAX_LISTED_PRODUCTS
    if remaining > 0:
        lines.append(_("… and {k} more").format(k=remaining))
    lines.extend(("", _("Details with /errori.")))
    return "\n".join(lines)


def operational_buttons(group: NoticeGroup) -> list[list[dict[str, str]]]:
    """Return pure callback-button data for a suspended group, or no buttons for warnings."""
    if group.event != "suspended" or not group.events:
        return []
    _title, _headline, _hint, delete_first = _copy_for(group.primary_reason)
    count = len(group.events)
    reactivate = {
        "text": _("▶️ Reactivate and recheck ({n})").format(n=count),
        "callback_data": encode(Action("ops.reactivate", (group.anchor_product_id,))),
    }
    delete = {
        "text": _("🗑 Delete all ({n})").format(n=count),
        "callback_data": encode(Action("ops.delete", (group.anchor_product_id,))),
    }
    return [[delete], [reactivate]] if delete_first else [[reactivate], [delete]]


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
    """Format a price-drop alert as Telegram HTML, in the current language."""
    sym = _currency_symbol(alert.currency)
    old = alert.old_price
    new = alert.new_price
    drop = old - new
    drop_pct = (drop / old * 100) if old > 0 else Decimal("0")
    return _(
        "📉 <b>Price drop!</b>\n\n"
        "<b>{name}</b>\n"
        '<a href="{url}">View product</a>\n\n'
        "Was: <s>{old} {symbol}</s>\n"
        "Now: <b>{new} {symbol}</b>\n"
        "Drop: -{drop} {symbol} ({percent}%)"
    ).format(
        name=_escape_html(alert.product_name),
        url=_escape_html(alert.url),
        old=old,
        new=new,
        drop=drop,
        symbol=sym,
        percent=f"{drop_pct:.1f}",
    )


def format_back_in_stock(*, product_name: str, url: str, price: Decimal, currency: str) -> str:
    """Announce, in the current language, that a sold-out listing is purchasable again."""
    return _(
        "📦 <b>Back in stock!</b>\n\n"
        "<b>{name}</b>\n"
        '<a href="{url}">View product</a>\n\n'
        "Price: <b>{price} {symbol}</b>"
    ).format(
        name=_escape_html(product_name),
        url=_escape_html(url),
        price=price,
        symbol=_currency_symbol(currency),
    )


# Deprecated: scheduled for removal once callers stop using this bridge.
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
        until = "\n" + _("🔁 I will retry by myself after: {when} UTC").format(
            when=f"{locked_until:%Y-%m-%d %H:%M}"
        )
    return _(
        "🔒 <b>Site paused automatically</b>\n\n"
        "<b>{domain}</b> failed too many checks ({reason}).\n"
        "I am pausing the checks on this site for a while, so as not to push "
        "against a block.{until}\n\n"
        "Details with /errori."
    ).format(domain=_escape_html(domain), reason=_escape_html(reason), until=until)
