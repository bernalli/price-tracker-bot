"""Inline-button callback dispatcher.

The original `handle_callback` was a single ~700-LOC if/elif chain. Task 17
splits it into per-domain helpers under this package and keeps the
dispatcher itself a thin sequence of `if handled := await ...: return`
calls.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram.ext import Application, CallbackQueryHandler

from price_tracker.bot.decorators import _db, with_locale
from price_tracker.bot.handlers.callbacks import _actions, _admin, _menu, _ops, _product

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


@with_locale
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch every inline-button click to the matching per-domain helper."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db = _db(context)
    if not await db.is_user_allowed(user_id):
        return

    data = query.data

    # Order matters — earlier handlers have higher specificity. Each helper
    # returns True when it handled the callback; on False we fall through.
    if await _ops.handle_ops_buttons(query, context, db, user_id, data):
        return
    if await _product.handle_delete_flow(query, context, db, user_id, data):
        return
    if await _product.handle_check_button(query, context, db, user_id, data):
        return
    if await _product.handle_chart_button(query, context, db, user_id, data):
        return
    if await _product.handle_amazon_pref(query, context, db, user_id, data):
        return
    if await _product.handle_track_choice(query, context, db, user_id, data):
        return
    if await _actions.handle_edit_button(query, context, db, user_id, data):
        return
    if await _actions.handle_pause_button(query, context, db, user_id, data):
        return
    if await _actions.handle_remove_button(query, context, db, user_id, data):
        return
    if await _actions.handle_reset_button(query, context, db, user_id, data):
        return
    if await _actions.handle_reactivate_button(query, context, db, user_id, data):
        return
    if await _menu.handle_menu_navigation(query, context, db, user_id, data):
        return
    if await _admin.handle_admin_menu(query, context, db, user_id, data):
        return
    if await _actions.handle_picker(query, context, db, user_id, data):
        return

    logger.info("Unhandled callback data: %s", data)


def register(app: Application) -> None:
    """Register the inline-button callback dispatcher on `app`."""
    app.add_handler(CallbackQueryHandler(handle_callback))


__all__ = ["handle_callback", "register"]
