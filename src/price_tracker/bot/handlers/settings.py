"""Settings handler: /intervallo (admin global check interval).

Ported from monolithic bot.py [item 17].
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from price_tracker.bot.decorators import _config, _db, admin_only
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes


@admin_only
async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the global price-check interval (admin)."""
    if not context.args:
        config = _config(context)
        await update.message.reply_text(
            f"⏱ Intervallo attuale: <b>ogni {config.check_interval_minutes} minuti</b>\n\n"
            f"Uso: /intervallo &lt;minuti&gt;\n"
            f"Esempio: <code>/intervallo 120</code> per ogni 2 ore",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        minutes = int(context.args[0])
    except ValueError:
        await update.message.reply_text(_("❌ Valore non valido."))
        return
    if minutes < 5:
        await update.message.reply_text(_("❌ L'intervallo minimo è 5 minuti."))
        return
    if minutes > 1440 * 7:
        await update.message.reply_text(_("❌ L'intervallo massimo è 7 giorni."))
        return

    config = _config(context)
    config.check_interval_minutes = minutes
    await _db(context).set_config("check_interval_minutes", str(minutes))

    if minutes >= 60:
        hours = minutes / 60
        display = f"{hours:.0f} ore" if hours == int(hours) else f"{hours:.1f} ore"
    else:
        display = f"{minutes} minuti"
    await update.message.reply_text(
        f"✅ Intervallo aggiornato: <b>ogni {display}</b>",
        parse_mode=ParseMode.HTML,
    )


def register(app: Application) -> None:
    """Register settings command handlers on `app`."""
    app.add_handler(CommandHandler("intervallo", cmd_set_interval))
    app.add_handler(CommandHandler("setinterval", cmd_set_interval))
