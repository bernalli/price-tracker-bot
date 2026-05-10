"""Admin user-management handlers: /adduser, /removeuser, /users, /nick.

Ported from monolithic bot.py [item 17].
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from price_tracker.bot.decorators import _db, admin_only, with_locale
from price_tracker.bot.handlers._helpers import _escape_html
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes


@with_locale
@admin_only
async def cmd_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a user. Usage: /adduser <telegram_id>"""
    if not context.args:
        await update.message.reply_text(
            _(
                "❌ Usage: /adduser &lt;telegram_id&gt;\n\n"
                "The user must send /start to the bot first to obtain their ID."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        new_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(_("❌ Invalid ID. Must be a number."))
        return

    db = _db(context)
    existing = await db.get_user(new_user_id)
    if existing and existing.get("is_active"):
        await update.message.reply_text(
            _("ℹ️ User <code>{tg_id}</code> is already authorized.").format(tg_id=new_user_id),
            parse_mode=ParseMode.HTML,
        )
        return

    await db.add_user(new_user_id, is_admin=False)
    await update.message.reply_text(
        _(
            "✅ User <code>{tg_id}</code> added.\nThey can now use the bot by sending /start."
        ).format(tg_id=new_user_id),
        parse_mode=ParseMode.HTML,
    )

    # Try to notify the new user
    with contextlib.suppress(Exception):
        await context.bot.send_message(
            chat_id=new_user_id,
            text=_("🎉 You have been authorized to use Price Tracker Bot.\nSend /start to begin."),
        )


@with_locale
@admin_only
async def cmd_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a user. Usage: /removeuser <telegram_id>"""
    if not context.args:
        await update.message.reply_text(
            _("❌ Usage: /removeuser &lt;telegram_id&gt;"),
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(_("❌ Invalid ID."))
        return

    db = _db(context)

    # Prevent removing yourself
    if target_id == update.effective_user.id:
        await update.message.reply_text(_("❌ You cannot remove yourself."))
        return

    # Prevent removing other admins
    if await db.is_user_admin(target_id):
        await update.message.reply_text(_("❌ You cannot remove another administrator."))
        return

    removed = await db.remove_user(target_id)
    if removed:
        await update.message.reply_text(
            _("✅ User <code>{tg_id}</code> removed.").format(tg_id=target_id)
            + "\n"
            + _(
                "Their products remain in the database"
                " but they will no longer receive notifications."
            ),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            _("❌ User <code>{tg_id}</code> not found.").format(tg_id=target_id),
            parse_mode=ParseMode.HTML,
        )


@with_locale
@admin_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all authorized users."""
    db = _db(context)
    users = await db.get_all_users()

    if not users:
        await update.message.reply_text(_("📭 No registered users."))
        return

    lines = [_("<b>👥 Authorized Users</b>\n")]
    for u in users:
        uid = u["user_id"]
        display = u.get("display_name") or ""
        uname = u.get("username") or ""
        name = display or uname or _("N/A")
        name_extra = f" (@{uname})" if uname and display else ""
        role = _("👑 Admin") if u.get("is_admin") else _("👤 User")
        # Count their products
        stats = await db.get_stats(uid)
        lines.append(
            _(
                "  {role} — <code>{uid}</code>\n"
                "    Name: {name}{name_extra}\n"
                "    Products: {active} active / {total} total"
            ).format(
                role=role,
                uid=uid,
                name=_escape_html(str(name)),
                name_extra=name_extra,
                active=stats["active_products"],
                total=stats["total_products"],
            )
        )

    await update.message.reply_text(
        "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@with_locale
@admin_only
async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set nickname for a user. Usage: /nick <id> <nome>"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            _(
                "❌ Usage: /nick &lt;id&gt; &lt;name&gt;\n\n"
                "Example: <code>/nick 123456789 Alice</code>"
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(_("❌ Invalid ID."))
        return
    nickname = " ".join(context.args[1:])
    db = _db(context)
    user = await db.get_user(target_id)
    if not user:
        await update.message.reply_text(
            _("❌ User <code>{tg_id}</code> not found.").format(tg_id=target_id),
            parse_mode=ParseMode.HTML,
        )
        return
    await db.update_user_info(target_id, display_name=nickname)
    await update.message.reply_text(
        _("✅ Nickname: <b>{nick}</b>\nID: <code>{tg_id}</code>").format(
            nick=_escape_html(nickname), tg_id=target_id
        ),
        parse_mode=ParseMode.HTML,
    )


def register(app: Application) -> None:
    """Register auth-domain command handlers on `app`."""
    app.add_handler(CommandHandler("adduser", cmd_add_user))
    app.add_handler(CommandHandler("removeuser", cmd_remove_user))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("utenti", cmd_users))
    app.add_handler(CommandHandler("nick", cmd_nick))
