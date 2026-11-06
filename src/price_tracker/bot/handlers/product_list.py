"""`/lista` handler — the user's tracked products, one paginated message.

Split out of `handlers/product.py` to keep each module under a 500-line
budget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram.ext import Application, CommandHandler, ContextTypes

from price_tracker.bot import list_media
from price_tracker.bot.decorators import _db, restricted, with_locale
from price_tracker.bot.handlers._cards import (
    default_interval,
    empty_list_text,
    list_view,
    reply_screen,
)
from price_tracker.bot.ui.list_text import list_page

if TYPE_CHECKING:
    from telegram import Update

logger = logging.getLogger(__name__)


@with_locale
@restricted
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the first page of the user's tracked products."""
    records = await _db(context).get_all_products(update.effective_user.id)
    if not records:
        await update.message.reply_text(empty_list_text())
        return
    interval = await default_interval(context)
    page = list_view(records, "a", 1, default_interval_minutes=interval)
    screen = list_page(page)
    mode = list_media.two_message_mode(context)
    if mode is not None:
        try:
            await list_media.send_list(update.message, mode, screen, page.items)
        except Exception:  # noqa: BLE001 — the optional grid must never cost the user the list
            logger.exception("list with grid photo failed; sending the text list")
        else:
            return
    await reply_screen(update.message, screen)


def register(app: Application) -> None:
    """Register the /lista command handlers on `app`."""
    app.add_handler(CommandHandler("lista", cmd_list))
    app.add_handler(CommandHandler("list", cmd_list))
