"""Price alert formatting and threshold trigger logic."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.messages import N_, _, current_locale, ngettext
from price_tracker.bot.ui.product_rows import product_row
from price_tracker.core.drop_floor import DropFloor, clears_drop_floor
from price_tracker.core.notices import (
    MAX_LISTED_PRODUCTS,
    NoticeGroup,
    OperationalEvent,
)
from price_tracker.core.textlimits import (
    DOMAIN_BUDGET,
    WHY_BUDGET,
    truncate_visible,
)
from price_tracker.i18n.format import money

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
    N_("A reliable price could not be obtained. The page layout or offer may have changed."),
    N_("Reactivate to run a fresh check right now."),
    False,
)

_REASON_COPY: dict[str, tuple[str, str, str, bool]] = {
    "listing_gone": (
        N_("Product pages unavailable on {domain}"),
        N_(
            "The store repeatedly returned a missing-page response. "
            "The listings may have been removed or moved."
        ),
        N_("Tracking is paused. Check the links before reactivating or deleting these products."),
        False,
    ),
    "listing_unconfirmed": (
        N_("Product pages could not be verified on {domain}"),
        N_(
            "Checks kept failing. The latest response suggests a missing page, "
            "but its status is unconfirmed."
        ),
        N_("Tracking is paused. Check the links and reactivate to try again."),
        False,
    ),
    "parse_error": _UNREADABLE_COPY,
    "price_none": _UNREADABLE_COPY,
    "no_scraper": _UNREADABLE_COPY,
    "condition_mismatch": _UNREADABLE_COPY,
    "implausible_read": _UNREADABLE_COPY,
    "currency_unknown": (
        N_("Price currency unclear on {domain}"),
        N_("The currency could not be verified. The last confirmed price has been preserved."),
        N_("Check the store's currency and reactivate to try again."),
        False,
    ),
    "http_error": (
        N_("Site unreachable: {domain}"),
        N_("The checks could not be completed because of connection or HTTP errors."),
        N_("Try again later. Reactivate once the site is back."),
        False,
    ),
    "unexpected": (
        N_("Checks interrupted on {domain}"),
        N_(
            "An internal error prevented the checks from completing. "
            "The last confirmed price has been preserved."
        ),
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
        if status_match is not None:
            return _("page not found (HTTP {status})").format(status=status_match.group(1))
        return _("product page unavailable")
    if reason == "listing_unconfirmed":
        return _("page status unconfirmed")
    if reason == "currency_unknown":
        return _("currency not verified")
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
    if reason == "http_error":
        return _("communication or processing error")
    if reason == "unexpected":
        return _("check interrupted")
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
        ),
        f"#{event.product_id}: "
        f"{_escape_html(truncate_visible(_why(event.reason, event.detail), WHY_BUDGET))}"
        + (f" ({event.error_count}/{event.max_errors})" if event.event == "warning" else ""),
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
    lines = [
        f"⏳ <b>{_('Checks failing on {domain}').format(domain=domain)}</b> ({len(group.events)})",
        "",
        ngettext(
            "{n} product needs attention. Repeated failures may suspend tracking.",
            "{n} products need attention. Repeated failures may suspend tracking.",
            len(group.events),
        ).format(n=len(group.events)),
        "",
    ]
    for event in group.events[:MAX_LISTED_PRODUCTS]:
        lines.extend(_product_lines(event))
    remaining = len(group.events) - MAX_LISTED_PRODUCTS
    if remaining > 0:
        lines.append(_("… and {k} more").format(k=remaining))
    lines.extend(("", _("Details with /errori.")))
    return "\n".join(lines)


def format_stock_notice(event: OperationalEvent) -> str:
    """Describe a confirmed stock transition without inventing a current price."""
    name = _escape_html(truncate_visible(event.product_name or event.url, 180))
    url = _escape_html(event.url)
    if event.event == "unavailable":
        title = _("Product currently unavailable")
        detail = _(
            "The store reported this product unavailable in repeated checks. "
            "Monitoring continues; you will be notified when it is available again."
        )
    else:
        title = _("Product available again")
        detail = _("The store reports this product available again with a verified price.")
        if event.last_price is not None:
            detail += "\n" + _("Last recorded price: {price}").format(
                price=money(event.last_price, event.currency or "EUR", locale=current_locale())
            )
    return f'<b>{title}</b>\n\n#{event.product_id}: <a href="{url}">{name}</a>\n\n{detail}'


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


def should_alert(
    *,
    old: Decimal,
    new: Decimal,
    threshold_type: ThresholdType,
    threshold_value: Decimal,
    target_price: Decimal | None,
    floor: DropFloor,
) -> bool:
    """Whether the move from ``old`` to ``new`` is announced as a price drop.

    The product's rule must fire (its threshold, or a crossing of its target
    price) AND the drop must clear the minimum-drop floor, whatever the rule:
    a few cents off an "any drop" product are not worth a message.
    """
    threshold_hit = crosses_threshold(
        old=old, new=new, threshold_type=threshold_type, threshold_value=threshold_value
    )
    # A target is a crossing, not a state: alert when the price moves from
    # above it to at-or-below it, so a product parked under its target does
    # not re-announce itself every cooldown window.
    target_hit = target_price is not None and new <= target_price < old
    if not (threshold_hit or target_hit):
        return False
    return clears_drop_floor(old=old, new=new, floor=floor)


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
    ).format(domain=_escape_html(domain), reason=_("automated checks blocked"), until=until)
