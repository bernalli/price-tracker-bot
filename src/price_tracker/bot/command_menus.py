"""Telegram command menus: one short list for everyone, in each language."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonCommands
from telegram.error import TelegramError

from price_tracker.bot.commands import COMMANDS, MENU_COMMANDS
from price_tracker.bot.messages import get_translation
from price_tracker.i18n.locales import AVAILABLE_LANGUAGES

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from telegram import Bot

    from price_tracker.db.repository import Repository

logger = logging.getLogger(__name__)

_Scope = BotCommandScopeDefault | BotCommandScopeChat


def menu_commands(lang: str | None) -> list[BotCommand]:
    """The command menu in ``lang``: the ``MENU_COMMANDS``, in their order."""
    gettext = get_translation(lang).gettext
    by_name = {spec.name: spec for spec in COMMANDS}
    return [BotCommand(name, gettext(by_name[name].description)) for name in MENU_COMMANDS]


async def _attempt(call: Awaitable[object], what: str) -> None:
    """Run one Bot API call, logging a Telegram failure instead of raising it."""
    try:
        await call
    except TelegramError as exc:
        logger.warning("command menu update failed (%s): %s", what, exc)


def _client_languages() -> list[tuple[str, str | None]]:
    """Each catalogue language with the code Telegram keys its menu by (English has none)."""
    return [(lang, None if lang == "en" else lang) for lang in AVAILABLE_LANGUAGES]


async def _set_menus(bot: Bot, scope: _Scope) -> None:
    """Set the menu of ``scope`` in every language."""
    for lang, code in _client_languages():
        await _attempt(
            bot.set_my_commands(menu_commands(lang), scope=scope, language_code=code),
            f"set {scope.type}",
        )


async def _delete_menus(bot: Bot, scope: _Scope) -> None:
    """Drop the menu of ``scope`` in every language, so the global one applies."""
    for _lang, code in _client_languages():
        await _attempt(
            bot.delete_my_commands(scope=scope, language_code=code), f"delete {scope.type}"
        )


async def sync_command_menus(bot: Bot, repo: Repository, user_id: int | None = None) -> None:
    """Give every chat the one short command menu.

    With ``None`` the global lists and the Menu button are set, then the per-chat list of
    every stored user (inactive and former admins included) is dropped, so the global one
    applies. With ``user_id`` only that chat's list is dropped. A Telegram error is logged
    and never stops the remaining calls.
    """
    if user_id is not None and (type(user_id) is not int or user_id <= 0):
        raise ValueError(f"user_id must be a positive int, got {user_id!r}")
    if user_id is None:
        await _set_menus(bot, BotCommandScopeDefault())
        await _attempt(bot.set_chat_menu_button(menu_button=MenuButtonCommands()), "menu button")
        targets = [user.user_id for user in await repo.list_users()]
    else:
        targets = [user_id]
    for uid in targets:
        await _delete_menus(bot, BotCommandScopeChat(uid))
