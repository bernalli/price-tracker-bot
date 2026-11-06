"""The paginated product list: one message, two lines per product, a compact keyboard."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Final

from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.messages import _, current_locale
from price_tracker.bot.ui.cards import list_header, list_mark
from price_tracker.bot.ui.labels import button
from price_tracker.bot.ui.panels import add_button, home_button
from price_tracker.bot.ui.product_rows import _compact_change, amount
from price_tracker.bot.ui.screens import Button, Screen
from price_tracker.bot.ui.width import display_width, sanitize_label, truncate_to_width
from price_tracker.i18n.format import change, money

if TYPE_CHECKING:
    from price_tracker.app.views import ListPage, ProductView

LINE_WIDTH: Final = 34
"""Display cells of the name line, number included: what a narrow phone shows unwrapped."""
INDENT: Final = "    "
"""Puts the price under the name rather than under the number."""
_FILTER_KEYS: Final = ("a", "p", "e", "o")
_FILTER_ICONS: Final = ("✅", "⏸", "⚠️", "🚫")


def _name_line(number: int, view: ProductView) -> str:
    prefix = f"{number}."
    name = sanitize_label(view.name) or _("Product #{product_id}").format(product_id=view.id)
    shown = truncate_to_width(name, LINE_WIDTH - display_width(prefix) - 1)
    return f"<b>{prefix}</b> {escape(shown)}"


def _price_line(view: ProductView) -> str:
    current, initial = amount(view.current), amount(view.initial)
    if current is None:
        price = _("N/A")
    else:
        try:
            price = money(current, view.currency, locale=current_locale())
        except ValueError:
            price = f"{current:.2f}"
    delta = ""
    if initial and current is not None:
        try:
            delta = change(initial, current, locale=current_locale())
        except ValueError:
            delta = _compact_change(initial, current)
    parts = [f"<b>{escape(price)}</b>"]
    if delta and display_width(price + delta) <= LINE_WIDTH - 6:
        parts.append(escape(delta))
    mark = list_mark(view)
    if mark:
        parts.append(mark)
    return INDENT + "  ".join(parts)


def _filter_row(page: ListPage) -> tuple[Button, ...]:
    """One row of four: icon and count, the shown filter between dots."""
    counts = page.counts or (None,) * len(_FILTER_KEYS)
    buttons = []
    for key, icon, count in zip(_FILTER_KEYS, _FILTER_ICONS, counts, strict=True):
        label = icon if count is None else f"{icon} {count}"
        if key == page.filter:
            label = f"• {label} •"
        buttons.append(button(label, callback=encode(Action("list.page", (key, 1)))))
    return tuple(buttons)


def list_page(page: ListPage) -> Screen:
    """Render one page of the product list. Pure: the locale is the current one."""
    lines = [list_header(page)]
    for number, view in enumerate(page.items, 1):
        lines += ["", _name_line(number, view), _price_line(view)]
    if not page.items:
        lines += ["", _("Nothing here.")]
    rows: list[tuple[Button, ...]] = []
    if page.items:
        rows.append(
            tuple(
                button(
                    str(number),
                    callback=encode(Action("list.open", (page.filter, page.page, view.id))),
                )
                for number, view in enumerate(page.items, 1)
            )
        )
    if page.pages > 1:
        nav = []
        if page.page > 1:
            nav.append(
                button("◀️", callback=encode(Action("list.page", (page.filter, page.page - 1))))
            )
        nav.append(button(f"{page.page}/{page.pages}", callback=encode(Action("noop"))))
        if page.page < page.pages:
            nav.append(
                button("▶️", callback=encode(Action("list.page", (page.filter, page.page + 1))))
            )
        rows.append(tuple(nav))
    rows.append(_filter_row(page))
    if page.filter == "a" and page.total > 1:
        rows.append((button(_("🗑 Delete all"), callback=encode(Action("list.remove_all"))),))
    rows.append((add_button(), home_button()))
    return Screen(text="\n".join(lines), rows=tuple(rows), disable_link_preview=True)


# The grid photo pairs with the same screen; kept as a name for its callers.
list_text_page = list_page
