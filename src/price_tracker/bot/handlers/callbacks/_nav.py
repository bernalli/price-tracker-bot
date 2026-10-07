"""Navigation callbacks decoded through the action registry.

``handle_action`` serves only the actions in ``_HANDLERS``; every other registered
action, and every legacy payload, stays with the legacy chain.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter

from price_tracker.app.views import PrefsView
from price_tracker.bot.handlers._cards import screen_markup
from price_tracker.bot.ui.panels import QUIET_WINDOWS, settings_screen, settings_section_screen
from price_tracker.db.models import NotificationPrefs
from price_tracker.notifier.preferences import PreferencesManager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from telegram.ext import ContextTypes

    from price_tracker.bot.callbacks import Action
    from price_tracker.bot.ui.screens import Screen

logger = logging.getLogger(__name__)


async def _edit(query: Any, screen: Screen) -> None:
    """Replace the message with ``screen``; a stale or repeated press is not an error."""
    try:
        await query.edit_message_text(
            screen.text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=screen.disable_link_preview,
            reply_markup=screen_markup(screen),
        )
    except BadRequest as exc:
        # A second tap renders the same content, which Telegram refuses.
        if "message is not modified" not in str(exc).lower():
            logger.warning("Could not edit the message: %s", exc)
    except RetryAfter as exc:
        logger.warning("Telegram asked to retry after %s s; the press is dropped", exc.retry_after)


async def _prefs_view(db: Any, user_id: int) -> PrefsView:
    effective = await PreferencesManager(repo=db).resolve_global(user_id=user_id)
    return PrefsView(**dataclasses.asdict(effective))


async def _noop(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    """The page indicator: the button is inert, the head of the dispatcher already answered."""


async def _settings(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    await _edit(query, settings_screen(await _prefs_view(db, user_id), now=datetime.now(UTC)))


async def _show_section(query: Any, db: Any, user_id: int, section: str) -> None:
    view = await _prefs_view(db, user_id)
    await _edit(query, settings_section_screen(section, view, now=datetime.now(UTC)))


async def _settings_section(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    await _show_section(query, db, user_id, str(action.args[0]))


async def _write_prefs(db: Any, user_id: int, **changes: Any) -> None:
    """Change fields of the global row, keeping the rest: the upsert replaces the whole row."""
    existing = await db.get_notification_prefs(user_id=user_id, product_id=None)
    base = existing if existing is not None else NotificationPrefs(user_id=user_id)
    await db.upsert_notification_prefs(dataclasses.replace(base, **changes))


async def _set_mute(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    value = str(action.args[0])
    if value == "off":
        await _write_prefs(db, user_id, mute=False, mute_until=None)
    elif value == "0":
        await _write_prefs(db, user_id, mute=True, mute_until=None)
    else:
        until = datetime.now(UTC) + timedelta(hours=int(value))
        await _write_prefs(db, user_id, mute=True, mute_until=until)
    await _show_section(query, db, user_id, "mu")


async def _set_digest(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    enabled = action.args[0] == "on"
    await _write_prefs(db, user_id, digest_mode=enabled, digest_interval_minutes=60)
    await _show_section(query, db, user_id, "dg")


async def _set_quiet(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    # "off" has no window: both ends are cleared.
    start, end = QUIET_WINDOWS.get(str(action.args[0]), (None, None))
    await _write_prefs(db, user_id, quiet_hours_start=start, quiet_hours_end=end)
    await _show_section(query, db, user_id, "qh")


_HANDLERS: Final[dict[str, Callable[..., Awaitable[None]]]] = {
    "noop": _noop,
    "settings": _settings,
    "settings.section": _settings_section,
    "settings.mute": _set_mute,
    "settings.digest": _set_digest,
    "settings.quiet": _set_quiet,
}


async def handle_action(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> bool:
    """Run the handler of ``action``; ``False`` when the action is not served here."""
    handler = _HANDLERS.get(action.name)
    if handler is None:
        return False
    await handler(query, context, db, user_id, action)
    return True
