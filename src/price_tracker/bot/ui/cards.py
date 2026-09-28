"""The product card: the one screen this foundation renders end to end."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from price_tracker.bot.messages import _, current_locale, ngettext
from price_tracker.bot.ui.escape import escape_html
from price_tracker.bot.ui.labels import button, layout_rows
from price_tracker.bot.ui.screens import Button, Screen
from price_tracker.bot.ui.width import truncate_to_width
from price_tracker.core.textlimits import DOMAIN_BUDGET, NAME_BUDGET
from price_tracker.i18n.format import ago, change, duration, money, percent

if TYPE_CHECKING:
    from price_tracker.app.views import ProductView


@dataclass(frozen=True, slots=True)
class CardActions:
    """Callback data for the card buttons, already encoded by the caller."""

    check: str
    history: str
    toggle: str  # pause when active, reactivate when paused or suspended
    delete: str
    alert_rule: str
    interval: str
    back: str


def _isolated(text: str) -> str:
    """Wrap in U+2068 FIRST STRONG ISOLATE / U+2069 POP DIRECTIONAL ISOLATE.

    A hostile or right-to-left value is laid out on its own and can no longer
    reorder the text that follows it on the same row.
    """
    return f"\u2068{text}\u2069"


def _name_value(view: ProductView) -> str:
    if not view.name:
        return escape_html(_("Product #{product_id}").format(product_id=view.id))
    return _isolated(escape_html(truncate_to_width(view.name, NAME_BUDGET)))


def _domain_value(view: ProductView) -> str:
    if not view.domain:
        return ""
    return _isolated(escape_html(truncate_to_width(view.domain, DOMAIN_BUDGET)))


def _name_line(view: ProductView) -> str:
    return _("📦 <b>{name}</b>").format(name=_name_value(view))


def _status_word(status: str) -> str:
    if status == "active":
        return _("active")
    if status == "paused":
        return _("paused")
    return _("suspended")


def _now_line(view: ProductView, *, loc: str) -> str:
    if view.current is None:
        return _("💰 Now: —")
    price = money(view.current, view.currency, locale=loc)
    estimate_value = view.reference_estimate
    if estimate_value is None or view.currency == view.reference_currency:
        return _("💰 Now: {price}").format(price=price)
    estimate = money(estimate_value, view.reference_currency, locale=loc)
    return _("💰 Now: {price} ≈ {estimate}").format(price=price, estimate=estimate)


def _start_line(view: ProductView, *, loc: str) -> str | None:
    if view.initial is None:
        return None
    price = money(view.initial, view.currency, locale=loc)
    if view.current is None:
        return _("📌 Start: {price}").format(price=price)
    delta_text = change(view.initial, view.current, locale=loc)
    if not delta_text:
        return _("📌 Start: {price}").format(price=price)
    return _("📌 Start: {price} · {change}").format(price=price, change=delta_text)


def _alert_line(view: ProductView, *, loc: str) -> str:
    if view.threshold_type == "any_drop":
        return _("🔔 Alert: any drop")
    if view.threshold_type == "percentage":
        rendered = percent(view.threshold_value / 100, locale=loc)
        return _("🔔 Alert: drop of {percent} from start").format(percent=rendered)
    price = money(view.threshold_value, view.currency, locale=loc)
    if view.threshold_type == "absolute":
        return _("🔔 Alert: drop of {amount} from start").format(amount=price)
    return _("🔔 Alert: at or below {price}").format(price=price)


def _checks_line(view: ProductView, *, now: datetime, loc: str) -> str:
    interval = duration(view.check_interval_minutes or view.default_interval_minutes, locale=loc)
    if view.last_checked_at is None:
        return _("🔄 Checks: every {interval} · never").format(interval=interval)
    ago_text = ago(now - view.last_checked_at, locale=loc)
    if ago_text is None:
        return _("🔄 Checks: every {interval} · just now").format(interval=interval)
    return _("🔄 Checks: every {interval} · last {ago}").format(interval=interval, ago=ago_text)


def _errors_line(view: ProductView) -> str | None:
    if view.consecutive_errors <= 0:
        return None
    return ngettext(
        "⚠️ {n} failed read · /errors",
        "⚠️ {n} failed reads · /errors",
        view.consecutive_errors,
    ).format(n=view.consecutive_errors)


def _keyboard(view: ProductView, actions: CardActions) -> tuple[tuple[Button, ...], ...]:
    toggle_label = _("⏸ Pause") if view.status == "active" else _("▶️ Reactivate")
    group_1 = [
        button(_("🔍 Check now"), callback=actions.check),
        button(_("📊 History"), callback=actions.history),
        button(toggle_label, callback=actions.toggle),
        button(_("🗑 Delete"), callback=actions.delete),
        button(_("🔔 Alert rule"), callback=actions.alert_rule),
        button(_("🔄 Interval"), callback=actions.interval),
    ]
    if view.url:
        group_1.append(button(_("🔗 Open"), url=view.url))
    group_2 = [button(_("◀️ List"), callback=actions.back)]
    return layout_rows(group_1, group_2)


def product_card(view: ProductView, actions: CardActions, *, now: datetime) -> Screen:
    """Render the product card. Pure: the caller supplies encoded callbacks and the clock."""
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError(f"now: must be an aware datetime, got {now!r}")
    loc = current_locale()

    lines = [
        _name_line(view),
        _("{domain} · #{id} · {status}").format(
            domain=_domain_value(view),
            id=view.id,
            status=_status_word(view.status),
        ),
        "",
        _now_line(view, loc=loc),
    ]
    start_line = _start_line(view, loc=loc)
    if start_line is not None:
        lines.append(start_line)
    if view.lowest is not None:
        lines.append(
            _("📉 Lowest: {price}").format(price=money(view.lowest, view.currency, locale=loc))
        )
    if view.target is not None:
        lines.append(
            _("🎯 Target: {price}").format(price=money(view.target, view.currency, locale=loc))
        )
    lines.append(_alert_line(view, loc=loc))
    lines.append(_checks_line(view, now=now, loc=loc))
    errors_line = _errors_line(view)
    if errors_line is not None:
        lines.append(errors_line)

    return Screen(text="\n".join(lines), rows=_keyboard(view, actions))
