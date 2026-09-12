"""Main-menu callback handlers (non-admin).

Split out of `handlers/callbacks/__init__.py` to keep the dispatcher under
the 500-LOC budget [Task 17].
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

from price_tracker.bot.decorators import _config
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_threshold,
    _safe_dec,
)
from price_tracker.bot.keyboards import menu_back_button
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Export column headers are a data contract, not UI: they stay untranslated so
# a CSV exported under one locale still imports under another. `cmd_import`
# also accepts the legacy Italian headers (see product_io.CSV_ALIASES).
CSV_HEADERS = [
    "ID",
    "Name",
    "URL",
    "Initial Price",
    "Current Price",
    "Lowest Price",
    "Target",
    "Threshold",
    "Active",
    "Currency",
]


async def handle_menu_navigation(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the non-admin menu callbacks (`menu_*`, `cmd_lista`).

    Returns `True` if the callback was a known menu action; `False` lets
    the caller try the next handler.
    """
    if data == "cmd_lista":
        products = await db.get_active_products(user_id)
        back_kb = InlineKeyboardMarkup([[*menu_back_button()]])
        if not products:
            await query.edit_message_text(
                _("📭 You have no tracked products.\nPaste me a link to get started!"),
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

    if data == "menu_main":
        user = query.from_user
        is_admin = await db.is_user_admin(user.id)
        rows = [
            [InlineKeyboardButton(_("📦 Products"), callback_data="menu_prodotti")],
            [InlineKeyboardButton(_("🔍 Price check"), callback_data="menu_prezzi")],
            [InlineKeyboardButton(_("🔔 Notifications"), callback_data="menu_notifiche")],
            [InlineKeyboardButton(_("💾 Import / Export"), callback_data="menu_dati")],
            [InlineKeyboardButton(_("📊 Info and stats"), callback_data="menu_info")],
        ]
        if is_admin:
            rows.append([InlineKeyboardButton(_("👑 Admin"), callback_data="menu_admin")])
        await query.edit_message_text(
            _("📋 <b>Price Tracker menu</b>\n\nPick a category:"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data == "menu_prodotti":
        products = await db.get_active_products(user_id)
        all_prods = await db.get_all_products(user_id)
        paused = [p for p in all_prods if not p.get("is_active")]
        rows = []
        if products:
            for p in products[:10]:
                nm = (p.get("name") or "?")[:28]
                cur = _safe_dec(p.get("current_price"))
                tag = f" €{cur:.2f}" if cur else ""
                rows.append(
                    [InlineKeyboardButton(f"#{p['id']} {nm}{tag}", callback_data=f"edit_{p['id']}")]
                )
            if len(products) > 10:
                rows.append(
                    [
                        InlineKeyboardButton(
                            _("... {count} more → /list").format(count=len(products) - 10),
                            callback_data="cmd_lista",
                        )
                    ]
                )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        _("📭 No products — paste a link!"),
                        callback_data="menu_main",
                    )
                ]
            )
        if paused:
            rows.append(
                [
                    InlineKeyboardButton(
                        _("⏸ {count} paused → reactivate").format(count=len(paused)),
                        callback_data="menu_paused",
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

    if data == "menu_paused":
        all_prods = await db.get_all_products(user_id)
        paused = [p for p in all_prods if not p.get("is_active")]
        rows = []
        for p in paused[:10]:
            nm = (p.get("name") or "?")[:35]
            rows.append(
                [InlineKeyboardButton(f"▶️ #{p['id']} {nm}", callback_data=f"reactivate_{p['id']}")]
            )
        rows.append(menu_back_button())
        await query.edit_message_text(
            _("⏸ <b>Paused products</b> ({count})\n\nTap one to reactivate it.").format(
                count=len(paused)
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data == "menu_prezzi":
        products = await db.get_active_products(user_id)
        rows = [[InlineKeyboardButton(_("🔄 Check all prices"), callback_data="menu_checkall")]]
        for p in products[:8]:
            nm = (p.get("name") or "?")[:30]
            rows.append(
                [InlineKeyboardButton(f"🔍 #{p['id']} {nm}", callback_data=f"check_{p['id']}")]
            )
        if products:
            rows.append([InlineKeyboardButton(_("📊 Price history"), callback_data="menu_storia")])
        rows.append(menu_back_button())
        await query.edit_message_text(
            _("🔍 <b>Price check</b>\n\nTap a product to check it."),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data == "menu_checkall":
        return await _handle_menu_checkall(query, context, db, user_id)

    if data == "menu_storia":
        products = await db.get_active_products(user_id)
        rows = []
        for p in products[:10]:
            nm = (p.get("name") or "?")[:35]
            rows.append(
                [InlineKeyboardButton(f"📊 #{p['id']} {nm}", callback_data=f"chart_{p['id']}")]
            )
        rows.append(menu_back_button())
        await query.edit_message_text(
            _("📊 <b>Price history</b>\n\nTap a product for its chart."),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data == "menu_notifiche":
        products = await db.get_active_products(user_id)
        rows = []
        for p in products[:10]:
            nm = (p.get("name") or "?")[:22]
            th = _format_threshold(
                p.get("threshold_type", "percentage"),
                p.get("threshold_value", "10"),
            )
            tgt = _safe_dec(p.get("target_price"))
            t_str = f" 🎯€{tgt:.0f}" if tgt else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        f"#{p['id']} {nm} [{th}]{t_str}",
                        callback_data=f"edit_{p['id']}",
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

    if data == "menu_dati":
        stats = await db.get_stats(user_id)
        rows = [
            [InlineKeyboardButton(_("💾 Export CSV"), callback_data="menu_esporta")],
            [
                InlineKeyboardButton(
                    _("📂 Import CSV — send the file in chat"),
                    callback_data="menu_importa_info",
                )
            ],
            menu_back_button(),
        ]
        await query.edit_message_text(
            _(
                "💾 <b>Import / Export</b>\n\n"
                "📦 {active} active, {total} total\n"
                "🔍 {checks} checks run"
            ).format(
                active=stats["active_products"],
                total=stats["total_products"],
                checks=stats["total_checks"],
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data == "menu_esporta":
        return await _handle_menu_esporta(query, db, user_id)

    if data == "menu_importa_info":
        await query.edit_message_text(
            _(
                "📂 <b>Import products</b>\n\n"
                "Send a CSV file in chat (the one produced by Export).\n"
                "Duplicates will be skipped."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([menu_back_button()]),
        )
        return True

    if data == "menu_info":
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
    await query.edit_message_text(_("🔍 Checking {count} products...").format(count=len(products)))
    from price_tracker.core.alert import format_alert  # noqa: PLC0415

    scheduler = context.bot_data["scheduler"]
    # Interactive caller: small per-product pause (see cmd_checkall in monitoring.py).
    results = await scheduler.check_user_products_for_user(
        user_id=user_id, delay_between_products=0.5
    )
    alerts = [r.alert for r in results if r.alert is not None]
    updated = await db.get_active_products(user_id)
    txt_lines = [_("✅ <b>Done</b> — {count} products").format(count=len(updated)) + chr(10)]
    for p in updated:
        nm = (p.get("name") or "?")[:35]
        cur = _safe_dec(p.get("current_price"))
        ini = _safe_dec(p.get("initial_price"))
        tag = f"€{cur:.2f}" if cur else _("N/A")
        diff = ""
        if ini and cur and ini > 0 and ini != cur:
            d = (ini - cur) / ini * 100
            diff = f" <i>(-{d:.1f}%)</i>" if d > 0 else f" <i>(+{abs(d):.1f}%)</i>"
        txt_lines.append(f"  #{p['id']} {_escape_html(nm)} — {tag}{diff}")
    if alerts:
        txt_lines.append(chr(10) + _("🔔 <b>{count} changes!</b>").format(count=len(alerts)))
    await query.edit_message_text(
        chr(10).join(txt_lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([menu_back_button()]),
    )
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
    w.writerow(CSV_HEADERS)
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
                "Yes" if p.get("is_active") else "No",
                p.get("currency", "EUR"),
            ]
        )
    await query.message.reply_document(
        document=InputFile(
            io.BytesIO(buf.getvalue().encode("utf-8")),
            filename=f"products_{datetime.now().strftime('%Y%m%d')}.csv",
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
        "📊 <b>Stats</b>\n\n"
        "📦 Active products: {active}\n"
        "📁 Total: {total}\n"
        "🔍 Checks: {checks}\n"
        "⏱ Interval: every {interval}"
    ).format(
        active=stats["active_products"],
        total=stats["total_products"],
        checks=stats["total_checks"],
        interval=int_str,
    )
    if is_admin:
        gs = await db.get_stats()
        users = await db.get_all_users()
        text += _(
            "\n\n👑 <b>Admin</b>\n"
            "👥 Users: {users}\n"
            "📦 Global products: {products}\n"
            "🔍 Global checks: {checks}"
        ).format(users=len(users), products=gs["active_products"], checks=gs["total_checks"])
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([menu_back_button()]),
    )
    return True
