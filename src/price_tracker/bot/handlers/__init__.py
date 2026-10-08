"""Aggregator — register all per-domain handlers on the Application.

Home commands (`/start`, `/menu`, `/help`) plus the global error handler
live here; per-domain handlers are imported from the sibling modules. The
guided-flow coordinator, which owns the threshold, target and interval prompts,
is registered in front of them.
"""

from __future__ import annotations

import dataclasses
import logging

from telegram import (
    InlineKeyboardButton,
    Update,
)
from telegram.ext import Application, CommandHandler, ContextTypes

from price_tracker.bot.callbacks import Action, encode
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
from price_tracker.bot.handlers._cards import home_view, reply_screen
from price_tracker.bot.handlers._helpers import _escape_html
from price_tracker.bot.messages import _, reset_locale, set_locale, user_locale
from price_tracker.bot.ui.panels import help_screen, home_screen

logger = logging.getLogger(__name__)


# ── Home commands ────────────────────────────────────────────────


@with_locale
@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/start` — a greeting above the Home screen."""
    user = update.effective_user
    screen = home_screen(await home_view(_db(context), user.id))
    greeting = _("👋 <b>Hello {first_name}!</b>").format(first_name=_escape_html(user.first_name))
    await reply_screen(
        update.message, dataclasses.replace(screen, text=f"{greeting}\n\n{screen.text}")
    )


@with_locale
@restricted
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/menu` — render the Home screen."""
    view = await home_view(_db(context), update.effective_user.id)
    await reply_screen(update.message, home_screen(view))


@with_locale
@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/help` — the command list, with the admin commands for administrators."""
    is_admin = await _db(context).is_user_admin(update.effective_user.id)
    await reply_screen(update.message, help_screen(is_admin))


def _menu_back_button() -> list[InlineKeyboardButton]:
    """Single-row 'back to main menu' button (legacy alias)."""
    return [InlineKeyboardButton(_("◀️ Menu"), callback_data=encode(Action("home")))]


# ── Error handler ─────────────────────────────────────────────────


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level exception handler — logs and notifies the user when possible."""
    import contextlib  # noqa: PLC0415 — keep top-level imports terse

    logger.error("Exception while handling update: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.message:
        user = update.effective_user
        language = user.language_code if user is not None else None
        if user is not None:
            with contextlib.suppress(Exception):
                language = await user_locale(_db(context), user.id, language)
        token = set_locale(language)
        try:
            with contextlib.suppress(Exception):
                await update.message.reply_text(
                    _("❌ An error occurred. Please try again in a moment.")
                )
        finally:
            reset_locale(token)


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
        # The repository is read when an update arrives: post_init stores it later.
        locale_resolver=lambda user_id, fallback: user_locale(
            app.bot_data["db"], user_id, fallback
        ),
        debug_runner=debug.debug_url,
    )
    register_guided_flow(app, flow, legacy_handlers_present=True)

    # Global error handler
    app.add_error_handler(error_handler)


__all__ = [
    "cmd_help",
    "cmd_menu",
    "cmd_start",
    "error_handler",
    "register_handlers",
]
