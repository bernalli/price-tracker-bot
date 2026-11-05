"""Numbered text lists paired with optional product grids."""

from __future__ import annotations

from typing import TYPE_CHECKING

from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.messages import _
from price_tracker.bot.ui.cards import list_header, list_mark
from price_tracker.bot.ui.labels import button, layout_rows
from price_tracker.bot.ui.panels import add_button
from price_tracker.bot.ui.product_rows import ROW_WIDTH, product_row
from price_tracker.bot.ui.screens import Button, Screen
from price_tracker.bot.ui.width import display_width

if TYPE_CHECKING:
    from price_tracker.app.views import ListPage


def list_text_page(page: ListPage) -> Screen:
    if page.counts is None:
        raise ValueError("counts are required for the numbered list")
    lines = [list_header(page), ""]
    rows: list[tuple[Button, ...]] = []
    for number, view in enumerate(page.items, 1):
        prefix = f"{number}. "
        lines.append(
            prefix
            + product_row(
                view.name[:120],
                view.current,
                initial=view.initial,
                currency=view.currency,
                mark=list_mark(view),
                width=ROW_WIDTH - display_width(prefix),
            )
        )
    if not page.items:
        lines.append(_("Nothing here."))
    else:
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
                button("⬅️", callback=encode(Action("list.page", (page.filter, page.page - 1))))
            )
        nav.append(button(f"{page.page}/{page.pages}", callback=encode(Action("noop"))))
        if page.page < page.pages:
            nav.append(
                button("▶️", callback=encode(Action("list.page", (page.filter, page.page + 1))))
            )
        rows.append(tuple(nav))
    keys = ("a", "p", "e", "o")
    icons = ("✅", "⏸", "⚠️", "🚫")
    words = (_("✅ Active"), _("⏸ Paused"), _("⚠️ Errors"), _("🚫 Sold out"))
    labels = [
        f"{words[index] if key == page.filter else icons[index]} {page.counts[index]}"
        for index, key in enumerate(keys)
    ]
    if sum(display_width(label) for label in labels) > 34:
        selected = keys.index(page.filter)
        labels[selected] = f"{icons[selected]} {page.counts[selected]} ✓"
    rows.append(
        tuple(
            button(label, callback=encode(Action("list.page", (key, 1))))
            for key, label in zip(keys, labels, strict=True)
        )
    )
    footer = [add_button()]
    if page.filter == "a" and page.total > 1:
        footer.append(button(_("🗑 Delete all"), callback=encode(Action("list.remove_all"))))
    footer.append(Button("🏠", callback=encode(Action("home"))))
    if sum(display_width(item.label) for item in footer) <= 34:
        rows.append(tuple(footer))
    else:
        rows.extend(layout_rows(footer))
    return Screen(text="\n".join(lines), rows=tuple(rows), disable_link_preview=True)
