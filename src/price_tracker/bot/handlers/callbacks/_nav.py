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
from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.handlers._cards import (
    card_actions,
    default_interval,
    empty_list_text,
    home_view,
    list_view,
    product_view,
    screen_markup,
)
from price_tracker.bot.messages import _, reset_locale, set_locale, user_locale
from price_tracker.bot.ui.cards import list_page, product_card
from price_tracker.bot.ui.panels import (
    QUIET_WINDOWS,
    help_screen,
    home_button,
    home_screen,
    settings_screen,
    settings_section_screen,
)
from price_tracker.bot.ui.screens import Screen
from price_tracker.i18n.locales import AVAILABLE_LANGUAGES
from price_tracker.notifier.preferences import PreferencesManager, update_prefs

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from telegram.ext import ContextTypes

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


async def _stored_language(db: Any, user_id: int) -> str | None:
    user = await db.get_user(user_id)
    return None if user is None else user.language


async def _settings(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    view = await _prefs_view(db, user_id)
    language = await _stored_language(db, user_id)
    await _edit(query, settings_screen(view, now=datetime.now(UTC), language=language))


async def _show_section(query: Any, db: Any, user_id: int, section: str) -> None:
    view = await _prefs_view(db, user_id)
    language = await _stored_language(db, user_id)
    screen = settings_section_screen(section, view, now=datetime.now(UTC), language=language)
    await _edit(query, screen)


async def _settings_section(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    await _show_section(query, db, user_id, str(action.args[0]))


async def _set_mute(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    value = str(action.args[0])
    if value == "off":
        await update_prefs(db, user_id, None, mute=False, mute_until=None)
    elif value == "0":
        await update_prefs(db, user_id, None, mute=True, mute_until=None)
    else:
        until = datetime.now(UTC) + timedelta(hours=int(value))
        await update_prefs(db, user_id, None, mute=True, mute_until=until)
    await _show_section(query, db, user_id, "mu")


async def _set_digest(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    enabled = action.args[0] == "on"
    await update_prefs(db, user_id, None, digest_mode=enabled)
    await _show_section(query, db, user_id, "dg")


async def _set_quiet(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    # "off" has no window: both ends are cleared.
    start, end = QUIET_WINDOWS.get(str(action.args[0]), (None, None))
    await update_prefs(db, user_id, None, quiet_hours_start=start, quiet_hours_end=end)
    await _show_section(query, db, user_id, "qh")


async def _set_language(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    value = str(action.args[0])
    if value == "auto":
        await db.set_user_language(user_id, None)
    elif value in AVAILABLE_LANGUAGES:
        await db.set_user_language(user_id, value)
    # A registered code without a catalogue writes nothing; the section is redrawn as is.
    # The section is drawn in the language now in effect, for this render only.
    language = await user_locale(db, user_id, query.from_user.language_code)
    token = set_locale(language)
    try:
        await _show_section(query, db, user_id, "lang")
    finally:
        reset_locale(token)


async def _list_screen(
    context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, list_filter: str, page: int
) -> Screen:
    records = await db.get_all_products(user_id)
    if not records:
        return Screen(text=empty_list_text(), rows=((home_button(),),))
    interval = await default_interval(context)
    return list_page(list_view(records, list_filter, page, default_interval_minutes=interval))


def _with_notice(screen: Screen, notice: str) -> Screen:
    """``screen`` with ``notice`` as its first line, a blank line before the content."""
    if not notice:
        return screen
    return dataclasses.replace(screen, text=f"{notice}\n\n{screen.text}")


async def show_list(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    db: Any,
    user_id: int,
    *,
    list_filter: str = "a",
    page: int = 1,
    notice: str = "",
) -> None:
    """Redraw the message as a page of ``user_id``'s list, with an optional notice on top."""
    screen = await _list_screen(context, db, user_id, list_filter, page)
    await _edit(query, _with_notice(screen, notice))


async def show_card(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    record: Any,
    *,
    back: str | None = None,
    notice: str = "",
) -> None:
    """Redraw the message as the card of ``record``, with an optional notice on top.

    Without ``back`` the List button opens page 1 of the filter the product is in.
    """
    view = product_view(record, default_interval_minutes=await default_interval(context))
    if back is None:
        back = encode(Action("list.page", ("a" if view.status == "active" else "p", 1)))
    screen = product_card(view, card_actions(view, back=back), now=datetime.now(UTC))
    await _edit(query, _with_notice(screen, notice))


async def _list_page(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    list_filter, page = action.args
    await show_list(query, context, db, user_id, list_filter=str(list_filter), page=int(page))


async def _list_open(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    list_filter, page, product_id = action.args
    # The list is personal: no admin exception, a foreign id is an unknown id.
    record = await db.get_product_for_user(int(product_id), user_id)
    if record is None:
        await show_list(
            query,
            context,
            db,
            user_id,
            list_filter=str(list_filter),
            page=int(page),
            notice=_("Product not found."),
        )
        return
    await show_card(query, context, record, back=encode(Action("list.page", (list_filter, page))))


async def _product_card(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    # Personal like the list: a foreign id is an unknown id.
    record = await db.get_product_for_user(int(action.args[0]), user_id)
    if record is None:
        await show_list(query, context, db, user_id, notice=_("Product not found."))
        return
    await show_card(query, context, record)


async def _home(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    await _edit(query, home_screen(await home_view(db, user_id)))


async def _help(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    await _edit(query, help_screen(await db.is_user_admin(user_id)))


_HANDLERS: Final[dict[str, Callable[..., Awaitable[None]]]] = {
    "noop": _noop,
    "settings": _settings,
    "settings.section": _settings_section,
    "settings.mute": _set_mute,
    "settings.digest": _set_digest,
    "settings.quiet": _set_quiet,
    "settings.language": _set_language,
    "list.page": _list_page,
    "list.open": _list_open,
    "product.card": _product_card,
    "home": _home,
    "help": _help,
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
