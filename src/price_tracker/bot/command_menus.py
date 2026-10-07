"""Telegram command menus: one list per language, a longer one for administrators."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.error import TelegramError

from price_tracker.bot.commands import COMMANDS
from price_tracker.bot.messages import get_translation
from price_tracker.i18n.locales import AVAILABLE_LANGUAGES

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from telegram import Bot

    from price_tracker.db.repository import Repository

logger = logging.getLogger(__name__)

_Scope = BotCommandScopeDefault | BotCommandScopeChat


def menu_commands(lang: str | None, *, admin: bool) -> list[BotCommand]:
    """The command menu in ``lang``: user commands, plus the admin ones when ``admin``."""
    gettext = get_translation(lang).gettext
    return [
        BotCommand(spec.name, gettext(spec.description))
        for spec in COMMANDS
        if admin or not spec.admin
    ]


async def _attempt(call: Awaitable[object], what: str) -> None:
    """Run one Bot API call, logging a Telegram failure instead of raising it."""
    try:
        await call
    except TelegramError as exc:
        logger.warning("command menu update failed (%s): %s", what, exc)


def _client_languages() -> list[tuple[str, str | None]]:
    """Each catalogue language with the code Telegram keys its menu by (English has none)."""
    return [(lang, None if lang == "en" else lang) for lang in AVAILABLE_LANGUAGES]


async def _set_menus(bot: Bot, scope: _Scope, *, admin: bool) -> None:
    """Set the menu of ``scope`` in every language."""
    for lang, code in _client_languages():
        await _attempt(
            bot.set_my_commands(menu_commands(lang, admin=admin), scope=scope, language_code=code),
            f"set {scope.type}",
        )


async def _delete_menus(bot: Bot, scope: _Scope) -> None:
    """Drop the menu of ``scope`` in every language, so the global one applies."""
    for _lang, code in _client_languages():
        await _attempt(
            bot.delete_my_commands(scope=scope, language_code=code), f"delete {scope.type}"
        )


async def sync_command_menus(bot: Bot, repo: Repository, user_id: int | None = None) -> None:
    """Bring Telegram's menus in line with the database.

    With ``user_id`` only that chat is refreshed; with ``None`` the global lists first, then
    every stored user. Active admins get the long list, everyone else the global one. A
    Telegram error is logged and never stops the remaining calls.
    """
    if user_id is not None and (type(user_id) is not int or user_id <= 0):
        raise ValueError(f"user_id must be a positive int, got {user_id!r}")
    if user_id is None:
        await _set_menus(bot, BotCommandScopeDefault(), admin=False)
        users = await repo.list_users()
        targets = {user.user_id: user.is_admin and user.is_active for user in users}
    else:
        user = await repo.get_user(user_id)
        targets = {user_id: bool(user and user.is_admin and user.is_active)}
    for uid, is_admin in targets.items():
        scope = BotCommandScopeChat(uid)
        if is_admin:
            await _set_menus(bot, scope, admin=True)
        else:
            await _delete_menus(bot, scope)
