"""Operational-notice callbacks for domain-scoped automatic suspensions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.decorators import _convert_display
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _get_user_product,
    _parse_id,
    _safe_dec,
)
from price_tracker.bot.handlers.callbacks._legacy import resolve_callback
from price_tracker.bot.messages import _
from price_tracker.bot.ui.width import truncate_to_width
from price_tracker.core.alert import _why
from price_tracker.core.notices import group_key_for
from price_tracker.core.textlimits import split_message

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


async def _reply_chunked(query: Any, text: str) -> None:
    """Replace the callback message, replying with any overflow chunks."""
    chunks = split_message(text)
    if not chunks:
        return
    await query.edit_message_text(chunks[0], parse_mode=ParseMode.HTML)
    for chunk in chunks[1:]:
        await query.message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def _automatic_group(
    context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, anchor_id: int
) -> tuple[Any | None, str, list[Any]]:
    """Return the visible anchor and current automatic group for its domain."""
    anchor = await _get_user_product(context, anchor_id, user_id)
    if anchor is None:
        return None, "", []
    domain = group_key_for(anchor.get("url", ""))
    suspended = await db.list_auto_suspended_products(user_id=user_id)
    group = [product for product in suspended if group_key_for(product.get("url", "")) == domain]
    return anchor, domain, group


async def _load_group(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    db: Any,
    user_id: int,
    action: Action,
) -> tuple[str, list[Any]] | None:
    """Parse and authorize an anchor, replying with the common error text."""
    raw = action.args[0]
    anchor_id = raw if isinstance(raw, int) else _parse_id(raw)
    if anchor_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return None
    anchor, domain, group = await _automatic_group(context, db, user_id, anchor_id)
    if anchor is None:
        await query.edit_message_text(_("❌ Product not found."))
        return None
    if not group:
        await query.edit_message_text(
            _("❌ Nothing to do: no automatically suspended products on this site.")
        )
        return None
    return domain, group


async def _handle_reactivate(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> bool:
    """Reactivate the automatic group, then ask the pull-mode scheduler to recheck it."""
    loaded = await _load_group(query, context, db, user_id, action)
    if loaded is None:
        return True
    domain, group = loaded
    await query.edit_message_text(
        _("⏳ Reactivating {n} products and checking them...").format(n=len(group))
    )
    product_ids = [product.id for product in group]
    for product_id in product_ids:
        await db.reactivate_product(product_id)

    scheduler = context.bot_data["scheduler"]
    results = await scheduler.check_products_for_user(
        product_ids=product_ids,
        user_id=user_id,
        delay_between_products=0.5,
    )
    lines = [
        _("▶️ <b>Rechecked {n} products on {domain}</b>").format(
            n=len(group), domain=_escape_html(domain)
        )
    ]
    for index, original in enumerate(group):
        product = await db.get_product(original.id)
        name_source = product.get("name") if product is not None else original.get("name")
        url_source = product.get("url") if product is not None else original.get("url")
        name = _escape_html(truncate_to_width(str(name_source or url_source), 60))
        result = results[index] if index < len(results) else None
        reason = getattr(result, "reason", None)
        current_price = _safe_dec(product.get("current_price")) if product is not None else None
        if reason is None and current_price is not None:
            currency = str(product.get("currency", "EUR"))
            price = _convert_display(current_price, currency)
            lines.append(_("✅ {name} — {price}").format(name=name, price=_escape_html(price)))
        else:
            lines.append(
                _("❌ {name} — {why}").format(
                    name=name,
                    why=_escape_html(_why(reason, None)),
                )
            )
    await _reply_chunked(query, "\n".join(lines))
    return True


async def _handle_delete_prompt(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> bool:
    """Show the non-destructive confirmation prompt for an automatic group."""
    loaded = await _load_group(query, context, db, user_id, action)
    if loaded is None:
        return True
    domain, group = loaded
    raw = action.args[0]
    anchor_id = raw if isinstance(raw, int) else _parse_id(raw)
    assert anchor_id is not None
    count = len(group)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _("🗑 Yes, delete {n}").format(n=count),
                    callback_data=encode(Action("ops.delete_ok", (anchor_id,))),
                )
            ],
            [InlineKeyboardButton(_("❌ Cancel"), callback_data=encode(Action("delete.cancel")))],
        ]
    )
    await query.edit_message_text(
        _(
            "🗑 Delete {n} products on {domain} and their price history? This cannot be undone."
        ).format(
            n=count,
            domain=_escape_html(domain),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return True


async def _handle_delete_confirm(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, action: Action
) -> bool:
    """Recompute and delete only the current automatic group for the clicker."""
    loaded = await _load_group(query, context, db, user_id, action)
    if loaded is None:
        return True
    domain, group = loaded
    deleted = 0
    for product in group:
        if await db.delete_product(product.id, user_id=user_id):
            deleted += 1
    await _reply_chunked(
        query,
        _("🗑 <b>Deleted {n} products on {domain}.</b>").format(
            n=deleted,
            domain=_escape_html(domain),
        ),
    )
    return True


async def handle_ops_buttons(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: object
) -> bool:
    """Handle operational notice callbacks, returning ``False`` for other prefixes."""
    action = resolve_callback(data)
    if action is None:
        return False
    if action.name == "ops.reactivate":
        return await _handle_reactivate(query, context, db, user_id, action)
    if action.name == "ops.delete_ok":
        return await _handle_delete_confirm(query, context, db, user_id, action)
    if action.name == "ops.delete":
        return await _handle_delete_prompt(query, context, db, user_id, action)
    return False
