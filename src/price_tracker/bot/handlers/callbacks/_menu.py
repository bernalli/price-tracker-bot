"""Main-menu callback handlers (non-admin).

Split out of `handlers/callbacks/__init__.py` to keep the dispatcher under
a 500-line budget.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode

from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.decorators import _config
from price_tracker.bot.handlers._cards import home_view
from price_tracker.bot.handlers.callbacks._legacy import resolve_callback
from price_tracker.bot.handlers.callbacks._nav import _edit
from price_tracker.bot.keyboards import menu_back_button
from price_tracker.bot.messages import _
from price_tracker.bot.ui.panels import home_screen
from price_tracker.bot.ui.product_rows import record_row
from price_tracker.core.textlimits import split_message

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def handle_menu_navigation(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: object
) -> bool:
    """Handle the non-admin menu callbacks (`menu_*`, `cmd_lista`).

    Returns `True` if the callback was a known menu action; `False` lets
    the caller try the next handler.
    """
    action = resolve_callback(data)
    if action is None:
        return False
    if action.name == "products.command":
        products = await db.get_active_products(user_id)
        back_kb = InlineKeyboardMarkup([[*menu_back_button()]])
        if not products:
            await query.edit_message_text(
                _("📭 You have no tracked products.\nPaste a link to get started!"),
                reply_markup=back_kb,
            )
        else:
            await query.edit_message_text(
                _(
                    "📦 You have <b>{count}</b> tracked products.\nUse /list to see them all."
                ).format(count=len(products)),
                parse_mode=ParseMode.HTML,
                reply_markup=back_kb,
            )
        return True

    if action.name == "home":
        await _edit(query, home_screen(await home_view(db, user_id)))
        return True

    if action.name == "products":
        products = await db.get_active_products(user_id)
        all_prods = await db.get_all_products(user_id)
        paused = [p for p in all_prods if not p.get("is_active")]
        rows = []
        if products:
            for p in products[:10]:
                rows.append(
                    [
                        InlineKeyboardButton(
                            record_row(p, html=False),
                            callback_data=encode(Action("product.edit", (p["id"],))),
                        )
                    ]
                )
            if len(products) > 10:
                rows.append(
                    [
                        InlineKeyboardButton(
                            _("... {count} more → /list").format(count=len(products) - 10),
                            callback_data=encode(Action("products.command")),
                        )
                    ]
                )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        _("🏠 No products — paste a link!"),
                        callback_data=encode(Action("home")),
                    )
                ]
            )
        if paused:
            rows.append(
                [
                    InlineKeyboardButton(
                        _("⏸ {count} paused → reactivate").format(count=len(paused)),
                        callback_data=encode(Action("paused")),
                    )
                ]
            )
        rows.append(menu_back_button())
        await query.edit_message_text(
            _("📦 <b>Your products</b> ({count} active)\n\nTap a product to edit it.").format(
                count=len(products)
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if action.name == "paused":
        all_prods = await db.get_all_products(user_id)
        paused = [p for p in all_prods if not p.get("is_active")]
        rows = []
        for p in paused[:10]:
            rows.append(
                [
                    InlineKeyboardButton(
                        record_row(p, html=False),
                        callback_data=encode(Action("product.reactivate", (p["id"],))),
                    )
                ]
            )
        rows.append(menu_back_button())
        await query.edit_message_text(
            _("⏸ <b>Paused products</b> ({count})\n\nTap to reactivate.").format(count=len(paused)),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if action.name == "prices":
        products = await db.get_active_products(user_id)
        rows = [
            [
                InlineKeyboardButton(
                    _("🔄 Check all prices"), callback_data=encode(Action("check_all"))
                )
            ]
        ]
        for p in products[:8]:
            rows.append(
                [
                    InlineKeyboardButton(
                        record_row(p, html=False),
                        callback_data=encode(Action("product.check", (p["id"],))),
                    )
                ]
            )
        if len(products) > 8:
            rows.append(
                [
                    InlineKeyboardButton(
                        _("… {n} more → Products").format(n=len(products) - 8),
                        callback_data=encode(Action("list.page", ("a", 1))),
                    )
                ]
            )
        if products:
            rows.append(
                [
                    InlineKeyboardButton(
                        _("📈 Price history"), callback_data=encode(Action("history"))
                    )
                ]
            )
        rows.append(menu_back_button())
        await query.edit_message_text(
            _("💶 <b>Price check</b>\n\nTap a product to check it."),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if action.name == "check_all":
        return await _handle_menu_checkall(query, context, db, user_id)

    if action.name == "history":
        products = await db.get_active_products(user_id)
        rows = []
        for p in products[:10]:
            rows.append(
                [
                    InlineKeyboardButton(
                        record_row(p, html=False),
                        callback_data=encode(Action("product.chart", (p["id"], "all"))),
                    )
                ]
            )
        rows.append(menu_back_button())
        await query.edit_message_text(
            _("📈 <b>Price history</b>\n\nTap a product to see its chart."),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if action.name == "notifications":
        products = await db.get_active_products(user_id)
        rows = []
        for p in products[:10]:
            rows.append(
                [
                    InlineKeyboardButton(
                        record_row(p, html=False),
                        callback_data=encode(Action("product.edit", (p["id"],))),
                    )
                ]
            )
        rows.append(menu_back_button())
        await query.edit_message_text(
            _("🔔 <b>Notifications</b>\n\nTap a product to change its threshold or target."),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if action.name == "data":
        stats = await db.get_stats(user_id)
        rows = [
            [InlineKeyboardButton(_("💾 Export CSV"), callback_data=encode(Action("data.export")))],
            [
                InlineKeyboardButton(
                    _("📥 Import CSV — send a file in chat"),
                    callback_data=encode(Action("data.import")),
                )
            ],
            menu_back_button(),
        ]
        await query.edit_message_text(
            _(
                "💾 <b>Import / Export</b>\n\n"
                "📦 {active} active, {total} total\n"
                "🔄 {checks} checks performed"
            ).format(
                active=stats["active_products"],
                total=stats["total_products"],
                checks=stats["total_checks"],
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if action.name == "data.export":
        return await _handle_menu_esporta(query, db, user_id)

    if action.name == "data.import":
        await query.edit_message_text(
            _(
                "📥 <b>Import products</b>\n\n"
                "Send a CSV file in chat (exported with Export).\n"
                "Duplicates will be skipped."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([menu_back_button()]),
        )
        return True

    if action.name == "stats":
        return await _handle_menu_info(query, context, db, user_id)

    return False


async def _handle_menu_checkall(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int
) -> bool:
    """Run /checkall via the menu button."""
    products = await db.get_active_products(user_id)
    if not products:
        await query.edit_message_text(
            _("📭 No products."),
            reply_markup=InlineKeyboardMarkup([menu_back_button()]),
        )
        return True
    await query.edit_message_text(_("🔄 Checking {count} products...").format(count=len(products)))
    from price_tracker.core.alert import format_alert  # noqa: PLC0415

    scheduler = context.bot_data["scheduler"]
    # Interactive caller: small per-product pause (see cmd_checkall in monitoring.py).
    results = await scheduler.check_user_products_for_user(
        user_id=user_id, delay_between_products=0.5
    )
    alerts = [r.alert for r in results if r.alert is not None]
    updated = await db.get_active_products(user_id)
    txt_lines = [
        _("✅ <b>Complete</b> — {count} products").format(count=len(updated)).replace(" — ", "\n")
        + chr(10)
    ]
    for p in updated:
        txt_lines.append(record_row(p))
    if alerts:
        txt_lines.append(chr(10) + _("🔔 <b>{count} changes!</b>").format(count=len(alerts)))
    pages = split_message(chr(10).join(txt_lines))
    await query.edit_message_text(
        pages[0],
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([menu_back_button()]),
    )
    for page in pages[1:]:
        await query.message.reply_text(page, parse_mode=ParseMode.HTML)
    for a in alerts:
        await query.message.reply_text(
            format_alert(a), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    return True


async def _handle_menu_esporta(query: Any, db: Any, user_id: int) -> bool:
    """Export CSV via the menu."""
    products = await db.get_all_products(user_id)
    if not products:
        await query.edit_message_text(
            _("📭 No products."),
            reply_markup=InlineKeyboardMarkup([menu_back_button()]),
        )
        return True
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "ID",
            "Nome",
            "URL",
            "Prezzo Iniziale",
            "Prezzo Attuale",
            "Prezzo Min",
            "Target",
            "Soglia",
            "Attivo",
            "Valuta",
        ]
    )
    for p in products:
        w.writerow(
            [
                p["id"],
                p.get("name", ""),
                p.get("url", ""),
                p.get("initial_price", ""),
                p.get("current_price", ""),
                p.get("lowest_price", ""),
                p.get("target_price", ""),
                f"{p.get('threshold_type', 'percentage')}:{p.get('threshold_value', '10')}",
                "Si" if p.get("is_active") else "No",
                p.get("currency", "EUR"),
            ]
        )
    await query.message.reply_document(
        document=InputFile(
            io.BytesIO(buf.getvalue().encode("utf-8")),
            filename=_("products_{date}.csv").format(date=datetime.now().strftime("%Y%m%d")),
        ),
        caption=_("💾 {count} products exported.").format(count=len(products)),
    )
    return True


async def _handle_menu_info(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int
) -> bool:
    """Render the user-facing stats panel."""
    config = _config(context)
    stats = await db.get_stats(user_id)
    is_admin = await db.is_user_admin(user_id)
    saved = await db.get_config("check_interval_minutes")
    interval = int(saved) if saved else config.check_interval_minutes
    int_str = f"{interval // 60}h" if interval >= 60 and interval % 60 == 0 else f"{interval}min"
    text = _(
        "ℹ️ <b>Statistics</b>\n\n"
        "📦 Active products: {active}\n"
        "📦 Total: {total}\n"
        "🔄 Checks: {checks}\n"
        "⏱ Interval: every {interval}"
    ).format(
        active=stats["active_products"],
        total=stats["total_products"],
        checks=stats["total_checks"],
        interval=int_str,
    )
    if is_admin:
        gs = await db.get_stats()
        users = await db.list_active_users()
        admin_block = _(
            "👑 <b>Admin</b>\n"
            "👥 Active users: {users}\n"
            "📦 Global products: {products}\n"
            "🔄 Global checks: {checks}"
        ).format(users=len(users), products=gs["active_products"], checks=gs["total_checks"])
        text += f"\n\n{admin_block}"
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(_("ℹ️ Help"), callback_data=encode(Action("help"))),
                    InlineKeyboardButton(_("⚠️ Errors"), callback_data=encode(Action("errors"))),
                ],
                menu_back_button(),
            ]
        ),
    )
    return True
