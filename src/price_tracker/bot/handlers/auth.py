"""Admin user-management handlers: /adduser, /removeuser, /users, /nick.

Ported from monolithic bot.py [item 17].
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from price_tracker.bot.decorators import _db, admin_only
from price_tracker.bot.handlers._helpers import _escape_html
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes


@admin_only
async def cmd_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a user. Usage: /adduser <telegram_id>"""
    if not context.args:
        await update.message.reply_text(
            _(
                "❌ Uso: /adduser &lt;telegram_id&gt;\n\n"
                "L'utente deve prima inviare /start al bot per ottenere il suo ID."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        new_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(_("❌ ID non valido. Deve essere un numero."))
        return

    db = _db(context)
    existing = await db.get_user(new_user_id)
    if existing and existing.get("is_active"):
        await update.message.reply_text(
            f"ℹ️ L'utente <code>{new_user_id}</code> è già autorizzato.",
            parse_mode=ParseMode.HTML,
        )
        return

    await db.add_user(new_user_id, is_admin=False)
    await update.message.reply_text(
        f"✅ Utente <code>{new_user_id}</code> aggiunto!\nOra può usare il bot inviando /start.",
        parse_mode=ParseMode.HTML,
    )

    # Try to notify the new user
    with contextlib.suppress(Exception):
        await context.bot.send_message(
            chat_id=new_user_id,
            text=_(
                "🎉 Sei stato autorizzato a usare il Price Tracker Bot!\nInvia /start per iniziare."
            ),
        )


@admin_only
async def cmd_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a user. Usage: /removeuser <telegram_id>"""
    if not context.args:
        await update.message.reply_text(
            "❌ Uso: /removeuser &lt;telegram_id&gt;",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(_("❌ ID non valido."))
        return

    db = _db(context)

    # Prevent removing yourself
    if target_id == update.effective_user.id:
        await update.message.reply_text(_("❌ Non puoi rimuovere te stesso."))
        return

    # Prevent removing other admins
    if await db.is_user_admin(target_id):
        await update.message.reply_text(_("❌ Non puoi rimuovere un altro amministratore."))
        return

    removed = await db.remove_user(target_id)
    if removed:
        await update.message.reply_text(
            f"✅ Utente <code>{target_id}</code> rimosso.\n"
            f"I suoi prodotti restano nel database ma non riceverà più notifiche.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"❌ Utente <code>{target_id}</code> non trovato.", parse_mode=ParseMode.HTML
        )


@admin_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all authorized users."""
    db = _db(context)
    users = await db.get_all_users()

    if not users:
        await update.message.reply_text(_("📭 Nessun utente registrato."))
        return

    lines = ["<b>👥 Utenti autorizzati</b>\n"]
    for u in users:
        uid = u["user_id"]
        display = u.get("display_name") or ""
        uname = u.get("username") or ""
        name = display or uname or "N/D"
        name_extra = f" (@{uname})" if uname and display else ""
        role = "👑 Admin" if u.get("is_admin") else "👤 Utente"
        # Count their products
        stats = await db.get_stats(uid)
        lines.append(
            f"  {role} — <code>{uid}</code>\n"
            f"    Nome: {_escape_html(str(name))}{name_extra}\n"
            f"    Prodotti: {stats['active_products']} attivi / "
            f"{stats['total_products']} totali"
        )

    await update.message.reply_text(
        "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set nickname for a user. Usage: /nick <id> <nome>"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Uso: /nick &lt;id&gt; &lt;nome&gt;\n\nEsempio: <code>/nick 123456789 Marco</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(_("❌ ID non valido."))
        return
    nickname = " ".join(context.args[1:])
    db = _db(context)
    user = await db.get_user(target_id)
    if not user:
        await update.message.reply_text(
            f"❌ Utente <code>{target_id}</code> non trovato.",
            parse_mode=ParseMode.HTML,
        )
        return
    await db.update_user_info(target_id, display_name=nickname)
    await update.message.reply_text(
        f"✅ Nickname: <b>{_escape_html(nickname)}</b>\nID: <code>{target_id}</code>",
        parse_mode=ParseMode.HTML,
    )


def register(app: Application) -> None:
    """Register auth-domain command handlers on `app`."""
    app.add_handler(CommandHandler("adduser", cmd_add_user))
    app.add_handler(CommandHandler("removeuser", cmd_remove_user))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("utenti", cmd_users))
    app.add_handler(CommandHandler("nick", cmd_nick))
