"""Admin-only callback handlers (`menu_admin*`, `admin_rm_*`, `admin_nick_*`).

Split out of `handlers/callbacks/__init__.py` to keep the dispatcher under
the 500-LOC budget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode

from price_tracker.bot.decorators import _config
from price_tracker.bot.handlers._helpers import _escape_html
from price_tracker.bot.keyboards import menu_back_button

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_BACK_TO_ADMIN = InlineKeyboardMarkup(
    [[InlineKeyboardButton("◀️ Impostazioni", callback_data="menu_admin")]]
)


async def handle_admin_menu(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the admin menu callbacks. Returns True if data was handled."""
    if data == "menu_admin":
        if not await db.is_user_admin(user_id):
            return True  # silent reject — handled
        users = await db.get_all_users()
        config = _config(context)
        saved = await db.get_config("check_interval_minutes")
        interval = int(saved) if saved else config.check_interval_minutes
        rows = [
            [InlineKeyboardButton("👥 Lista utenti", callback_data="menu_admin_users")],
            [
                InlineKeyboardButton("➕ Aggiungi utente", callback_data="menu_admin_adduser"),
                InlineKeyboardButton("🚫 Rimuovi utente", callback_data="menu_admin_removeuser"),
            ],
            [InlineKeyboardButton("✏️ Nickname utente", callback_data="menu_admin_nick")],
            [
                InlineKeyboardButton(
                    f"⏱ Intervallo globale: {interval} min",
                    callback_data="menu_admin_interval",
                )
            ],
            [InlineKeyboardButton("🔧 Debug scraper", callback_data="menu_admin_debug")],
            menu_back_button(),
        ]
        await query.edit_message_text(
            f"👑 <b>Impostazioni</b>\n\n"
            f"👥 Utenti attivi: {len(users)}\n"
            f"⏱ Intervallo globale: {interval} min",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data == "menu_admin_users":
        if not await db.is_user_admin(user_id):
            return True
        users = await db.get_all_users()
        txt = ["👥 <b>Utenti</b>\n"]
        for u in users:
            uid = u["user_id"]
            nm = u.get("display_name") or u.get("username") or "N/D"
            role = "👑" if u.get("is_admin") else "👤"
            st = await db.get_stats(uid)
            txt.append(
                f"{role} <code>{uid}</code> {_escape_html(str(nm))} — "
                f"{st['active_products']} prod."
            )
        await query.edit_message_text(
            chr(10).join(txt),
            parse_mode=ParseMode.HTML,
            reply_markup=_BACK_TO_ADMIN,
        )
        return True

    if data == "menu_admin_adduser":
        if not await db.is_user_admin(user_id):
            return True
        context.user_data["pending_action"] = ("admin_adduser", 0)
        await query.edit_message_text(
            "➕ <b>Aggiungi utente</b>\n\n" "Scrivi l'ID Telegram dell'utente da aggiungere:",
            parse_mode=ParseMode.HTML,
        )
        return True

    if data == "menu_admin_removeuser":
        if not await db.is_user_admin(user_id):
            return True
        users = await db.get_all_users()
        removable = [u for u in users if not u.get("is_admin") and u["user_id"] != user_id]
        if not removable:
            await query.edit_message_text(
                "❌ Nessun utente rimovibile.", reply_markup=_BACK_TO_ADMIN
            )
            return True
        rows = []
        for u in removable:
            nm = u.get("display_name") or u.get("username") or str(u["user_id"])
            rows.append(
                [InlineKeyboardButton(f"🚫 {nm}", callback_data=f"admin_rm_{u['user_id']}")]
            )
        rows.append([InlineKeyboardButton("◀️ Impostazioni", callback_data="menu_admin")])
        await query.edit_message_text(
            "🚫 <b>Rimuovi utente</b>\n\nTocca per rimuovere:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("admin_rm_"):
        if not await db.is_user_admin(user_id):
            return True
        target_id = int(data.replace("admin_rm_", ""))
        removed = await db.remove_user(target_id)
        if removed:
            await query.edit_message_text(
                f"✅ Utente <code>{target_id}</code> rimosso.",
                parse_mode=ParseMode.HTML,
                reply_markup=_BACK_TO_ADMIN,
            )
        else:
            await query.edit_message_text("❌ Utente non trovato.", reply_markup=_BACK_TO_ADMIN)
        return True

    if data == "menu_admin_nick":
        if not await db.is_user_admin(user_id):
            return True
        users = await db.get_all_users()
        rows = []
        for u in users:
            nm = u.get("display_name") or u.get("username") or str(u["user_id"])
            rows.append(
                [InlineKeyboardButton(f"✏️ {nm}", callback_data=f"admin_nick_{u['user_id']}")]
            )
        rows.append([InlineKeyboardButton("◀️ Impostazioni", callback_data="menu_admin")])
        await query.edit_message_text(
            "✏️ <b>Nickname</b>\n\nScegli utente:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("admin_nick_"):
        if not await db.is_user_admin(user_id):
            return True
        target_id = int(data.replace("admin_nick_", ""))
        context.user_data["pending_action"] = ("admin_nick", target_id)
        u = await db.get_user(target_id)
        current_name = u.get("display_name", "N/D") if u else "N/D"
        await query.edit_message_text(
            f"✏️ <b>Nickname per {target_id}</b>\n"
            f"Attuale: {_escape_html(str(current_name))}\n\n"
            "Scrivi il nuovo nickname:",
            parse_mode=ParseMode.HTML,
        )
        return True

    if data == "menu_admin_interval":
        if not await db.is_user_admin(user_id):
            return True
        context.user_data["pending_action"] = ("admin_interval", 0)
        await query.edit_message_text(
            "⏱ <b>Intervallo globale</b>\n\n"
            "Scrivi i minuti (es. <code>60</code>, <code>360</code>):",
            parse_mode=ParseMode.HTML,
        )
        return True

    if data == "menu_admin_debug":
        if not await db.is_user_admin(user_id):
            return True
        context.user_data["pending_action"] = ("admin_debug", 0)
        await query.edit_message_text(
            "🔧 <b>Debug scraper</b>\n\n" "Incolla l'URL del prodotto da analizzare:",
            parse_mode=ParseMode.HTML,
        )
        return True

    return False
