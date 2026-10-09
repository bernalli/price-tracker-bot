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
from price_tracker.bot.handlers.debug import errors_text, health_text, no_errors_text
from price_tracker.bot.messages import _, reset_locale, set_locale, user_locale
from price_tracker.bot.ui.cards import list_page, product_card
from price_tracker.bot.ui.labels import button, layout_rows
from price_tracker.bot.ui.panels import (
    QUIET_WINDOWS,
    add_button,
    add_screen,
    help_screen,
    home_button,
    home_screen,
    product_prefs_screen,
    settings_screen,
    settings_section_screen,
)
from price_tracker.bot.ui.screens import Button, Screen
from price_tracker.core.textlimits import split_message
from price_tracker.i18n.locales import AVAILABLE_LANGUAGES
from price_tracker.notifier.preferences import PreferencesManager, clear_mute, update_prefs

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


async def _prefs_view(db: Any, user_id: int, product_id: int | None = None) -> PrefsView:
    """The global preferences, or those in effect for ``product_id``."""
    manager = PreferencesManager(repo=db)
    if product_id is None:
        effective = await manager.resolve_global(user_id=user_id)
    else:
        effective = await manager.resolve(user_id=user_id, product_id=product_id)
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


async def _show_section(query: Any, db: Any, user_id: int, section: str, notice: str = "") -> None:
    view = await _prefs_view(db, user_id)
    language = await _stored_language(db, user_id)
    screen = settings_section_screen(section, view, now=datetime.now(UTC), language=language)
    await _edit(query, _with_notice(screen, notice))


async def _settings_section(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    await _show_section(query, db, user_id, str(action.args[0]))


def _mute_changes(value: str) -> dict[str, Any]:
    """The fields a mute preset writes: ``off``, ``0`` (forever) or a number of hours."""
    if value == "off":
        return {"mute": False, "mute_until": None}
    if value == "0":
        return {"mute": True, "mute_until": None}
    return {"mute": True, "mute_until": datetime.now(UTC) + timedelta(hours=int(value))}


async def _set_mute(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    await update_prefs(db, user_id, None, **_mute_changes(str(action.args[0])))
    await _show_section(query, db, user_id, "mu")


async def _digest_now(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    sent = await context.bot_data["digest_service"].flush_user(user_id=user_id)
    notice = _("Pending alerts sent: {n}").format(n=sent)
    await _show_section(query, db, user_id, "dg", notice=notice)


async def _show_product_prefs(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, record: Any
) -> None:
    product_id = int(record["id"])
    name = record.get("name") or _("Product #{product_id}").format(product_id=product_id)
    view = await _prefs_view(db, user_id, product_id)
    await _edit(query, product_prefs_screen(name, product_id, view, now=datetime.now(UTC)))


async def _product_prefs(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    # Personal like the card: a foreign id is an unknown id, for admins too.
    record = await db.get_product_for_user(int(action.args[0]), user_id)
    if record is None:
        await show_list(query, context, db, user_id, notice=_("Product not found."))
        return
    await _show_product_prefs(query, context, db, user_id, record)


async def _product_mute(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    product_id, value = int(action.args[0]), str(action.args[1])
    record = await db.get_product_for_user(product_id, user_id)
    if record is None:
        await show_list(query, context, db, user_id, notice=_("Product not found."))
        return
    if value == "off":
        # Only the product's own mute: a global mute still applies to it.
        await clear_mute(db, user_id, product_id)
    else:
        await update_prefs(db, user_id, product_id, **_mute_changes(value))
    await _show_product_prefs(query, context, db, user_id, record)


def _report_screen(text: str, back: Button) -> Screen:
    """A report cut to one message, with a way back and Home."""
    return Screen(text=split_message(text)[0], rows=layout_rows([back, home_button()]))


async def _admin_health(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    if not await db.is_user_admin(user_id):
        return  # silent, like the other admin buttons
    text = health_text(context.bot_data["health_manager"])
    await _edit(query, _report_screen(text, button(_("◀️ Admin"), callback=encode(Action("admin")))))


async def _errors(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    text = await errors_text(db, context.bot_data.get("health_manager"), user_id)
    back = button(_("◀️ Status & info"), callback=encode(Action("stats")))
    chunks = split_message(text or no_errors_text())
    await _edit(query, _report_screen(chunks[0], back))
    for chunk in chunks[1:]:
        await query.message.reply_text(chunk, parse_mode=ParseMode.HTML)


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
        return Screen(text=empty_list_text(), rows=layout_rows([add_button(), home_button()]))
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


async def _add(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> None:
    await _edit(query, add_screen())


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
    "settings.digest_now": _digest_now,
    "product.prefs": _product_prefs,
    "product.mute": _product_mute,
    "admin.health": _admin_health,
    "errors": _errors,
    "add": _add,
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
