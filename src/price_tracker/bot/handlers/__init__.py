"""Aggregator — register all per-domain handlers on the Application.

Home commands (`/start`, `/menu`, `/help`) plus the global error handler
live here per the Task 17 mapping; per-domain handlers are imported from
the sibling modules.
"""

from __future__ import annotations

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from price_tracker.bot.decorators import _db, restricted, with_locale
from price_tracker.bot.handlers import (
    auth,
    callbacks,
    debug,
    history,
    monitoring,
    product,
    product_io,
    product_list,
    settings,
    text_input,
)
from price_tracker.bot.handlers._helpers import _escape_html
from price_tracker.bot.messages import _

logger = logging.getLogger(__name__)


# ── Home commands ────────────────────────────────────────────────


@with_locale
@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/start` — welcome message."""
    user = update.effective_user
    await update.message.reply_text(
        _(
            "👋 <b>Hi {name}!</b>\n\n"
            "I track online prices and let you know when they drop.\n\n"
            "🚀 <b>To get started:</b> paste a link in the chat\n"
            "🎯 <b>Supported:</b> Amazon, eBay, Shopify and more\n"
            "🛡 <b>Amazon:</b> new/used and seller filters\n\n"
            "Press /menu for everything else."
        ).format(name=_escape_html(user.first_name)),
        parse_mode=ParseMode.HTML,
    )


@with_locale
@restricted
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/menu` — render the main menu."""
    db = _db(context)
    is_admin = await db.is_user_admin(update.effective_user.id)
    await _send_main_menu(update.message, is_admin)


# Alias
cmd_help = cmd_menu


async def _send_main_menu(message: object, is_admin: bool = False) -> None:
    """Render the main menu inline keyboard."""
    rows = [
        [
            InlineKeyboardButton(_("📦 Products"), callback_data="menu_prodotti"),
            InlineKeyboardButton(_("🔍 Prices"), callback_data="menu_prezzi"),
        ],
        [
            InlineKeyboardButton(_("🔔 Notifications"), callback_data="menu_notifiche"),
            InlineKeyboardButton(_("💾 Data"), callback_data="menu_dati"),
        ],
        [InlineKeyboardButton(_("📊 Status and info"), callback_data="menu_info")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(_("👑 Settings (admin)"), callback_data="menu_admin")])
    await message.reply_text(  # type: ignore[attr-defined]
        _("📋 <b>Menu</b>\n\nWhat do you want to do?"),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _menu_back_button() -> list[InlineKeyboardButton]:
    """Single-row 'back to main menu' button (legacy alias)."""
    return [InlineKeyboardButton(_("◀️ Menu"), callback_data="menu_main")]


# ── Error handler ─────────────────────────────────────────────────


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level exception handler — logs and notifies the user when possible."""
    import contextlib  # noqa: PLC0415 — keep top-level imports terse

    logger.error("Exception while handling update: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.message:
        with contextlib.suppress(Exception):
            await update.message.reply_text(
                _("❌ Something went wrong. Please try again in a moment.")
            )


# ── Aggregator ────────────────────────────────────────────────────


def register_handlers(app: Application) -> None:
    """Register every per-domain handler module on the application."""
    # Home commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))

    # Per-domain handlers (registration order intentionally mirrors the legacy bot)
    auth.register(app)
    product.register(app)
    product_list.register(app)
    product_io.register(app)
    monitoring.register(app)
    history.register(app)
    settings.register(app)
    debug.register(app)
    callbacks.register(app)
    # text_input must register AFTER all command handlers so the catch-all
    # filters don't shadow CommandHandler dispatch.
    text_input.register(app)

    # Global error handler
    app.add_error_handler(error_handler)


__all__ = [
    "_send_main_menu",
    "cmd_help",
    "cmd_menu",
    "cmd_start",
    "error_handler",
    "register_handlers",
]
