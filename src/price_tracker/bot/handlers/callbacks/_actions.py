"""Per-product action callbacks (`edit_*`, `pause_*`, `remove_*`, `reset_*`,
`reactivate_*`).

Split out of `handlers/callbacks/_product.py` to keep each module under a
500-line budget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_threshold,
    _get_user_product,
    _parse_id,
    _safe_dec,
)
from price_tracker.bot.handlers.callbacks._legacy import resolve_callback
from price_tracker.bot.handlers.callbacks._nav import show_card, show_list
from price_tracker.bot.messages import _
from price_tracker.bot.ui.width import truncate_to_width

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _product_id(data: object, name: str) -> tuple[bool, int | None]:
    action = resolve_callback(data)
    if action is None or action.name != name:
        return False, None
    raw = action.args[0]
    return True, raw if isinstance(raw, int) else _parse_id(raw)


async def handle_edit_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: object
) -> bool:
    """Handle the 'Modifica' button (`edit_<id>`)."""
    handled, product_id = _product_id(data, "product.edit")
    if not handled:
        return False
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text(_("❌ Product not found."))
        return True

    name = truncate_to_width(product.get("name") or _("Unknown"), 60)
    threshold_type = product.get("threshold_type", "percentage")
    threshold_value = product.get("threshold_value", "10")
    threshold_str = _format_threshold(threshold_type, threshold_value)
    target = _safe_dec(product.get("target_price"))
    target_str = f"€{target:.2f}" if target else _("not set")

    initial = _safe_dec(product.get("initial_price"))
    current = _safe_dec(product.get("current_price"))
    initial_str = f"€{initial:.2f}" if initial else _("N/A")

    edit_buttons = [
        [
            InlineKeyboardButton(
                _("🔔 Any drop"),
                callback_data=encode(Action("product.threshold_any", (product_id,))),
            )
        ],
        [
            InlineKeyboardButton(
                _("📉 Threshold % or €"),
                callback_data=encode(Action("product.threshold", (product_id,))),
            )
        ],
        [
            InlineKeyboardButton(
                _("💰 Target price"), callback_data=encode(Action("product.target", (product_id,)))
            )
        ],
    ]
    if initial and current and initial != current:
        edit_buttons.append(
            [
                InlineKeyboardButton(
                    _("🔄 Reset base price"),
                    callback_data=encode(Action("product.reset", (product_id,))),
                )
            ]
        )
    edit_buttons.append(
        [
            InlineKeyboardButton(
                _("🔔 Notifications"), callback_data=encode(Action("product.prefs", (product_id,)))
            )
        ]
    )

    await query.message.reply_text(
        _(
            "✏️ <b>Edit #{id}</b> {name}\n\n"
            "🎯 Current threshold: <b>{threshold}</b>\n"
            "🏁 Current target: <b>{target}</b>\n"
            "📌 Base price: <b>{base}</b>\n\n"
            "<b>What do you want to change?</b>"
        ).format(
            id=product_id,
            name=_escape_html(name),
            threshold=threshold_str,
            target=target_str,
            base=initial_str,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(edit_buttons),
    )
    return True


async def handle_pause_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: object
) -> bool:
    """Handle the 'Pausa' button (`pause_<id>`)."""
    handled, product_id = _product_id(data, "product.pause")
    if not handled:
        return False
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await show_list(query, context, db, user_id, notice=_("Product not found."))
        return True

    await db.deactivate_product(product_id)
    await _show_fresh_card(query, context, db, user_id, product_id, _("⏸ Tracking paused."))
    return True


async def _show_fresh_card(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    db: Any,
    user_id: int,
    product_id: int,
    notice: str,
) -> None:
    """Redraw the card from the stored row after an action; the list if the row is gone."""
    record = await db.get_product(product_id)
    if record is None:
        await show_list(query, context, db, user_id, notice=_("Product not found."))
        return
    await show_card(query, context, record, notice=notice)


async def handle_remove_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: object
) -> bool:
    """Handle the 'Elimina' button (`remove_<id>`) — shows confirmation prompt."""
    handled, product_id = _product_id(data, "product.remove")
    if not handled:
        return False
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await show_list(query, context, db, user_id, notice=_("Product not found."))
        return True

    name = truncate_to_width(product.get("name") or _("Unknown"), 50)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _("🗑 Yes, delete everything"),
                    callback_data=encode(Action("product.remove_ok", (product_id,))),
                ),
                InlineKeyboardButton(
                    _("⏸ Pause only"), callback_data=encode(Action("product.pause", (product_id,)))
                ),
                InlineKeyboardButton(
                    _("❌ Cancel"), callback_data=encode(Action("product.card", (product_id,)))
                ),
            ]
        ]
    )
    await query.edit_message_text(
        _("❓ What do you want to do with <b>{name}</b>?").format(name=_escape_html(name)),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return True


async def handle_reset_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: object
) -> bool:
    """Handle the 'Reset base price' button (`reset_<id>`)."""
    handled, product_id = _product_id(data, "product.reset")
    if not handled:
        return False
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text(_("❌ Product not found."))
        return True
    success = await db.reset_initial_price(product_id)
    if success:
        name = truncate_to_width(product.get("name") or _("Unknown"), 60)
        current = _safe_dec(product.get("current_price"))
        price_str = f"€{current:.2f}" if current else _("N/A")
        await query.edit_message_text(
            _(
                "✅ Base price updated!\n\n"
                "📦 <b>#{product_id}</b> {name}\n"
                "💰 New base: <b>{price}</b>"
            ).format(product_id=product_id, name=_escape_html(name), price=price_str),
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.edit_message_text(_("❌ Could not update."))
    return True


async def handle_reactivate_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: object
) -> bool:
    """Handle the 'Riattiva' button (`reactivate_<id>`)."""
    handled, product_id = _product_id(data, "product.reactivate")
    if not handled:
        return False
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await show_list(query, context, db, user_id, notice=_("Product not found."))
        return True
    await db.reactivate_product(product_id)
    await _show_fresh_card(query, context, db, user_id, product_id, _("▶️ Tracking resumed."))
    return True
