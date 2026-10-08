"""Admin-only callback handlers (`menu_admin*`, `admin_rm_*`, `admin_nick_*`).

Split out of `handlers/callbacks/__init__.py` to keep the dispatcher under
a 500-line budget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode

from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.decorators import _config
from price_tracker.bot.handlers._helpers import _escape_html, _parse_id
from price_tracker.bot.handlers.callbacks._legacy import resolve_callback
from price_tracker.bot.keyboards import menu_back_button
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _cancel_prompt() -> InlineKeyboardMarkup:
    """The Cancel button of a prompt waiting for typed text: back to the admin menu."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(_("Cancel"), callback_data=encode(Action("admin")))]]
    )


def _back_to_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(_("◀️ Settings"), callback_data=encode(Action("admin")))]]
    )


async def handle_admin_menu(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: object
) -> bool:
    """Handle the admin menu callbacks. Returns True if data was handled."""
    action = resolve_callback(data)
    if action is None:
        return False
    if action.name == "admin":
        if not await db.is_user_admin(user_id):
            return True  # silent reject — handled
        # Back here from a prompt: the next message is no longer an answer to it.
        context.user_data.pop("pending_action", None)
        users = await db.list_active_users()
        config = _config(context)
        saved = await db.get_config("check_interval_minutes")
        interval = int(saved) if saved else config.check_interval_minutes
        rows = [
            [InlineKeyboardButton(_("👥 User list"), callback_data=encode(Action("admin.users")))],
            [
                InlineKeyboardButton(
                    _("➕ Add user"), callback_data=encode(Action("admin.add_user"))
                ),
                InlineKeyboardButton(
                    _("🚫 Remove user"), callback_data=encode(Action("admin.remove_user"))
                ),
            ],
            [
                InlineKeyboardButton(
                    _("✏️ User nickname"), callback_data=encode(Action("admin.nick"))
                )
            ],
            [
                InlineKeyboardButton(
                    _("⏱ Global interval: {interval} min").format(interval=interval),
                    callback_data=encode(Action("admin.interval")),
                )
            ],
            [
                InlineKeyboardButton(
                    _("🔧 Debug scraper"), callback_data=encode(Action("admin.debug"))
                )
            ],
            [
                InlineKeyboardButton(
                    _("🏥 Scraper health"), callback_data=encode(Action("admin.health"))
                )
            ],
            menu_back_button(),
        ]
        await query.edit_message_text(
            _(
                "👑 <b>Admin</b>\n\n👥 Active users: {users}\n⏱ Global interval: {interval} min"
            ).format(users=len(users), interval=interval),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if action.name == "admin.users":
        if not await db.is_user_admin(user_id):
            return True
        users = await db.list_active_users()
        txt = [_("👥 <b>Active users</b>\n")]
        for u in users:
            uid = u["user_id"]
            nm = u.get("display_name") or u.get("username") or _("N/A")
            role = "👑" if u.get("is_admin") else "👤"
            st = await db.get_stats(uid)
            txt.append(
                _("{role} <code>{user_id}</code> {name} — {count} products").format(
                    role=role,
                    user_id=uid,
                    name=_escape_html(str(nm)),
                    count=st["active_products"],
                )
            )
        await query.edit_message_text(
            chr(10).join(txt),
            parse_mode=ParseMode.HTML,
            reply_markup=_back_to_admin(),
        )
        return True

    if action.name == "admin.add_user":
        if not await db.is_user_admin(user_id):
            return True
        context.user_data["pending_action"] = ("admin_adduser", 0)
        await query.edit_message_text(
            _("➕ <b>Add user</b>\n\nEnter the Telegram ID of the user to add:"),
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_prompt(),
        )
        return True

    if action.name == "admin.remove_user":
        if not await db.is_user_admin(user_id):
            return True
        users = await db.list_active_users()
        removable = [u for u in users if not u.get("is_admin") and u["user_id"] != user_id]
        if not removable:
            await query.edit_message_text(
                _("❌ No removable users."), reply_markup=_back_to_admin()
            )
            return True
        rows = []
        for u in removable:
            nm = u.get("display_name") or u.get("username") or str(u["user_id"])
            rows.append(
                [
                    InlineKeyboardButton(
                        f"🚫 {nm}",
                        callback_data=encode(Action("admin.remove_user_id", (u["user_id"],))),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(_("◀️ Settings"), callback_data=encode(Action("admin")))])
        await query.edit_message_text(
            _("🚫 <b>Remove user</b>\n\nTap a user to remove them:"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if action.name == "admin.remove_user_id":
        if not await db.is_user_admin(user_id):
            return True
        raw = action.args[0]
        target_id = raw if isinstance(raw, int) else _parse_id(raw)
        if target_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        if target_id == user_id:
            await query.edit_message_text(
                _("❌ You cannot remove yourself."), reply_markup=_back_to_admin()
            )
            return True
        if await db.is_user_admin(target_id):
            await query.edit_message_text(
                _("❌ You cannot remove another administrator."),
                reply_markup=_back_to_admin(),
            )
            return True
        removed = await db.remove_user(target_id)
        if removed:
            await query.edit_message_text(
                _("✅ User <code>{user_id}</code> removed.").format(user_id=target_id),
                parse_mode=ParseMode.HTML,
                reply_markup=_back_to_admin(),
            )
        else:
            await query.edit_message_text(_("❌ User not found."), reply_markup=_back_to_admin())
        return True

    if action.name == "admin.nick":
        if not await db.is_user_admin(user_id):
            return True
        users = await db.list_active_users()
        rows = []
        for u in users:
            nm = u.get("display_name") or u.get("username") or str(u["user_id"])
            rows.append(
                [
                    InlineKeyboardButton(
                        f"✏️ {nm}", callback_data=encode(Action("admin.nick_id", (u["user_id"],)))
                    )
                ]
            )
        rows.append([InlineKeyboardButton(_("◀️ Settings"), callback_data=encode(Action("admin")))])
        await query.edit_message_text(
            _("✏️ <b>Nickname</b>\n\nChoose a user:"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if action.name == "admin.nick_id":
        if not await db.is_user_admin(user_id):
            return True
        raw = action.args[0]
        target_id = raw if isinstance(raw, int) else _parse_id(raw)
        if target_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        context.user_data["pending_action"] = ("admin_nick", target_id)
        u = await db.get_user(target_id)
        current_name = u.get("display_name", _("N/A")) if u else _("N/A")
        await query.edit_message_text(
            _(
                "✏️ <b>Nickname for {user_id}</b>\nCurrent: {current}\n\nEnter the new nickname:"
            ).format(user_id=target_id, current=_escape_html(str(current_name))),
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_prompt(),
        )
        return True

    if action.name == "admin.interval":
        if not await db.is_user_admin(user_id):
            return True
        context.user_data["pending_action"] = ("admin_interval", 0)
        await query.edit_message_text(
            _(
                "⏱ <b>Global interval</b>\n\n"
                "Enter the minutes (e.g. <code>60</code>, <code>360</code>):"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_prompt(),
        )
        return True

    return False
