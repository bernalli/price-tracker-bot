"""Aggregator — register all per-domain handlers on the Application.

Home commands (`/start`, `/menu`, `/help`) plus the global error handler
live here; per-domain handlers are imported from the sibling modules. The
guided-flow coordinator, which owns the threshold, target and interval prompts,
is registered in front of them.
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
from price_tracker.bot.flow_services import RepositoryFlowServices
from price_tracker.bot.flows import FlowConfig, GuidedFlow, JobQueueTimer, register_guided_flow
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

logger = logging.getLogger(__name__)


# ── Home commands ────────────────────────────────────────────────


@with_locale
@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/start` — welcome message."""
    user = update.effective_user
    await update.message.reply_text(
        f"👋 <b>Ciao {_escape_html(user.first_name)}!</b>\n\n"
        "Monitoro i prezzi online e ti avviso quando scendono.\n\n"
        "🚀 <b>Per iniziare:</b> incolla un link in chat\n"
        "🎯 <b>Supporto:</b> Amazon, eBay, Shopify e altri\n"
        "🛡 <b>Amazon:</b> filtro nuovo/usato e venditore\n\n"
        "Premi /menu per tutte le funzioni.",
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
            InlineKeyboardButton("📦 Prodotti", callback_data="menu_prodotti"),
            InlineKeyboardButton("🔍 Prezzi", callback_data="menu_prezzi"),
        ],
        [
            InlineKeyboardButton("🔔 Notifiche", callback_data="menu_notifiche"),
            InlineKeyboardButton("💾 Dati", callback_data="menu_dati"),
        ],
        [InlineKeyboardButton("📊 Stato e info", callback_data="menu_info")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("👑 Impostazioni (admin)", callback_data="menu_admin")])
    await message.reply_text(  # type: ignore[attr-defined]
        "📋 <b>Menu</b>\n\nCosa vuoi fare?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _menu_back_button() -> list[InlineKeyboardButton]:
    """Single-row 'back to main menu' button (legacy alias)."""
    return [InlineKeyboardButton("◀️ Menu", callback_data="menu_main")]


# ── Error handler ─────────────────────────────────────────────────


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level exception handler — logs and notifies the user when possible."""
    import contextlib  # noqa: PLC0415 — keep top-level imports terse

    logger.error("Exception while handling update: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.message:
        with contextlib.suppress(Exception):
            await update.message.reply_text(
                "❌ Si è verificato un errore. Riprova tra qualche istante."
            )


# ── Aggregator ────────────────────────────────────────────────────


LEGACY_GROUP = 2


def _register_legacy(app: Application) -> None:
    """Register every per-domain handler module in group 0, in the historical order."""
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


def register_handlers(app: Application) -> None:
    """Register the guided-flow coordinator, ``/cancel`` and every legacy handler.

    Layout: group 0 the coordinator, group 1 ``/cancel``, group 2 the legacy
    handlers in their historical order. Python-telegram-bot runs at most one
    handler per group, so an update the coordinator closes and passes on (another
    command, a legacy button) still reaches its legacy handler.
    """
    if app.job_queue is None:
        raise RuntimeError("the guided-flow timeouts need a job queue")
    _register_legacy(app)
    for handler in list(app.handlers.get(0, ())):
        app.remove_handler(handler, 0)
        app.add_handler(handler, LEGACY_GROUP)
    flow = GuidedFlow(
        RepositoryFlowServices(lambda: app.bot_data["db"]),
        JobQueueTimer(app.job_queue),
        config=FlowConfig(add_entry=False),
    )
    register_guided_flow(app, flow, legacy_handlers_present=True)

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
