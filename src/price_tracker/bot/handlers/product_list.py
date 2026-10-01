"""`/lista` handler — list the user's tracked products with per-row buttons.

Split out of `handlers/product.py` to keep each module under a 500-line
budget.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from price_tracker.bot.decorators import _config, _db, restricted, with_locale
from price_tracker.bot.handlers._cards import card_actions, product_view, screen_markup
from price_tracker.bot.messages import _
from price_tracker.bot.ui.cards import product_card

logger = logging.getLogger(__name__)


@with_locale
@restricted
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the user's tracked products."""
    db = _db(context)
    user_id = update.effective_user.id
    products = await db.get_active_products(user_id)

    if not products:
        await update.message.reply_text(
            _("📭 Non hai prodotti tracciati.\nIncollami un link per iniziare!")
        )
        return

    await update.message.reply_text(
        f"<b>📦 I tuoi prodotti ({len(products)})</b>",
        parse_mode=ParseMode.HTML,
    )

    config = _config(context)
    saved = await db.get_config("check_interval_minutes")
    default_interval = int(saved) if saved and saved.isdigit() else config.check_interval_minutes
    now = datetime.now(UTC)
    for p in products:
        view = product_view(p, default_interval_minutes=default_interval)
        screen = product_card(view, card_actions(view), now=now)
        await update.message.reply_text(
            screen.text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=screen.disable_link_preview,
            reply_markup=screen_markup(screen),
        )

    # "Elimina tutti" button at the end
    if len(products) > 1:
        keyboard_all = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🗑 Elimina tutti i prodotti", callback_data="delete_all")]]
        )
        await update.message.reply_text(
            f"───────────────\n📦 <b>{len(products)}</b> prodotti tracciati",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_all,
        )


def register(app: Application) -> None:
    """Register the /lista command handlers on `app`."""
    app.add_handler(CommandHandler("lista", cmd_list))
    app.add_handler(CommandHandler("list", cmd_list))
