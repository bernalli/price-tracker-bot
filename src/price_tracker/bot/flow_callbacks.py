"""Registration and fallback callbacks for guided flows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from price_tracker.bot.callbacks import REGISTRY, InvalidCallback
from price_tracker.bot.flow_rendering import (
    TEXT_CANCELLED,
    TEXT_EXPIRED,
    TEXT_NO_OPEN_PROMPT,
    TEXT_NOTHING_TO_CANCEL,
)
from price_tracker.bot.flow_state import CANCEL_COMMAND, LEGACY_PENDING_KEY, AnyContext
from price_tracker.bot.messages import _, reset_locale, set_locale, user_locale

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application

    from price_tracker.bot.flow_coordinator import GuidedFlow

_MESSAGE_ONLY: Final = filters.UpdateType.MESSAGE


async def _sender_language(update: Update, context: AnyContext) -> str | None:
    """The stored language of the sender, else their Telegram language."""
    user = update.effective_user
    if user is None:
        return None
    return await user_locale(context.bot_data["db"], user.id, user.language_code)


async def nothing_to_cancel(update: Update, context: AnyContext) -> None:
    """Group 1: ``/cancel`` outside a guided flow.

    A prompt still armed by a legacy handler (``pending_action``) is disarmed and
    reported as cancelled; otherwise there is nothing to cancel.
    """
    if update.message is None:
        return
    locale = set_locale(await _sender_language(update, context))
    try:
        user_data = context.user_data
        if isinstance(user_data, dict) and LEGACY_PENDING_KEY in user_data:
            del user_data[LEGACY_PENDING_KEY]
            await update.message.reply_text(_(TEXT_CANCELLED))
            return
        await update.message.reply_text(_(TEXT_NOTHING_TO_CANCEL))
    finally:
        reset_locale(locale)


async def no_open_prompt(update: Update, context: AnyContext) -> None:
    """Group 3: free text that no flow asked for."""
    if update.message is None:
        return
    locale = set_locale(await _sender_language(update, context))
    try:
        await update.message.reply_text(_(TEXT_NO_OPEN_PROMPT))
    finally:
        reset_locale(locale)


async def expired_callback(update: Update, context: AnyContext) -> None:
    """Group 3 catch-all: callback data outside the registry gets the expiry toast."""
    del context
    if update.callback_query is not None:
        await update.callback_query.answer(_(TEXT_EXPIRED))


FLOW_GROUP: Final = 0
COMMAND_GROUP: Final = 1
FALLBACK_GROUP: Final = 3


def register_guided_flow(
    application: Application[Any, Any, Any, Any, Any, Any],
    flow: GuidedFlow,
    *,
    legacy_handlers_present: bool = False,
) -> None:
    """Install the coordinator (group 0), ``/cancel`` (group 1) and the fallbacks (group 3).

    While legacy handlers are registered (``legacy_handlers_present``), the two
    group-3 fallbacks are **not** installed: legacy text and callback handlers
    neither raise ``ApplicationHandlerStop`` nor report consumption, so a group-3
    fallback would answer a message or a callback query that a legacy handler
    already consumed (measured by the coexistence tests). Until the last legacy
    handler is deleted, the legacy free-text handler stays the only free-text
    consumer and unanswered callbacks keep today's behaviour.
    """
    flow.attach(application.bot)
    application.add_handler(flow, group=FLOW_GROUP)
    application.add_handler(
        CommandHandler(CANCEL_COMMAND, nothing_to_cancel, filters=_MESSAGE_ONLY),
        group=COMMAND_GROUP,
    )
    if legacy_handlers_present:
        return
    application.add_handler(
        CallbackQueryHandler(expired_callback, pattern=is_unregistered), group=FALLBACK_GROUP
    )
    application.add_handler(
        MessageHandler(_MESSAGE_ONLY & filters.TEXT & ~filters.COMMAND, no_open_prompt),
        group=FALLBACK_GROUP,
    )


def is_unregistered(data: object) -> bool:
    """Pattern of the group-3 catch-all: data outside the registered language.

    PTB evaluates every group unless a handler raises ``ApplicationHandlerStop``,
    so a pattern-less catch-all would answer a second time every callback that a
    group-1 action handler already answered (measured by the codec tests).
    """
    return isinstance(REGISTRY.decode(data), InvalidCallback)


def is_registered_non_flow(data: object) -> bool:
    """Pattern for group-1 action handlers: a registered action that is not a flow's."""
    decoded = REGISTRY.decode(data)
    return not isinstance(decoded, InvalidCallback) and not REGISTRY.is_flow_action(decoded)
