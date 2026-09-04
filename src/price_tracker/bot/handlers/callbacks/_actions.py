"""Per-product action callbacks (`edit_*`, `pause_*`, `remove_*`, `reset_*`,
`reactivate_*`, `set*_*` pickers).

Split out of `handlers/callbacks/_product.py` to keep each module under the
500-LOC budget [Task 17].
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_threshold,
    _get_user_product,
    _parse_id,
    _safe_dec,
)
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def handle_edit_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Edit' button (`edit_<id>`)."""
    if not data.startswith("edit_"):
        return False

    product_id = _parse_id(data.replace("edit_", ""))
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text(_("❌ Product not found."))
        return True

    name = (product.get("name") or _("Unknown"))[:60]
    threshold_type = product.get("threshold_type", "percentage")
    threshold_value = product.get("threshold_value", "10")
    threshold_str = _format_threshold(threshold_type, threshold_value)
    target = _safe_dec(product.get("target_price"))
    target_str = f"€{target:.2f}" if target else _("not set")

    initial = _safe_dec(product.get("initial_price"))
    current = _safe_dec(product.get("current_price"))
    initial_str = f"€{initial:.2f}" if initial else _("N/A")

    edit_buttons = [
        [InlineKeyboardButton(_("🔔 Every drop"), callback_data=f"track_any_{product_id}")],
        [
            InlineKeyboardButton(
                _("📉 Threshold % or €"), callback_data=f"track_threshold_{product_id}"
            )
        ],
        [InlineKeyboardButton(_("💰 Target price"), callback_data=f"track_target_{product_id}")],
    ]
    if initial and current and initial != current:
        edit_buttons.append(
            [InlineKeyboardButton(_("🔄 Reset base price"), callback_data=f"reset_{product_id}")]
        )

    await query.message.reply_text(
        _(
            "✏️ <b>Edit #{pid}</b> {name}\n\n"
            "🎯 Current threshold: <b>{threshold}</b>\n"
            "🏁 Current target: <b>{target}</b>\n"
            "📌 Base price: <b>{initial}</b>\n\n"
            "<b>What do you want to change?</b>"
        ).format(
            pid=product_id,
            name=_escape_html(name),
            threshold=threshold_str,
            target=target_str,
            initial=initial_str,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(edit_buttons),
    )
    return True


async def handle_pause_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Pause' button (`pause_<id>`)."""
    if not data.startswith("pause_"):
        return False

    product_id = _parse_id(data.replace("pause_", ""))
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text(_("❌ Product not found."))
        return True

    name = (product.get("name") or _("Unknown"))[:50]
    await db.deactivate_product(product_id)
    await query.edit_message_text(
        _("⏸ <b>Paused:</b> {name}\nUse /reactivate {pid} to resume it.").format(
            name=_escape_html(name), pid=product_id
        ),
        parse_mode=ParseMode.HTML,
    )
    return True


async def handle_remove_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Delete' button (`remove_<id>`) — shows confirmation prompt."""
    if not data.startswith("remove_"):
        return False

    product_id = _parse_id(data.replace("remove_", ""))
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text(_("❌ Product not found."))
        return True

    name = (product.get("name") or _("Unknown"))[:50]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _("🗑 Yes, delete everything"),
                    callback_data=f"confirm_delete_{product_id}",
                ),
                InlineKeyboardButton(_("⏸ Just pause"), callback_data=f"pause_{product_id}"),
                InlineKeyboardButton(_("❌ Cancel"), callback_data="cancel_delete"),
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
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Reset base price' button (`reset_<id>`)."""
    if not data.startswith("reset_"):
        return False

    product_id = _parse_id(data.replace("reset_", ""))
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text(_("❌ Product not found."))
        return True
    success = await db.reset_initial_price(product_id)
    if success:
        name = (product.get("name") or _("Unknown"))[:60]
        current = _safe_dec(product.get("current_price"))
        price_str = f"€{current:.2f}" if current else _("N/A")
        await query.edit_message_text(
            _(
                "✅ Base price updated!\n\n📦 <b>#{pid}</b> {name}\n💰 New base: <b>{price}</b>"
            ).format(pid=product_id, name=_escape_html(name), price=price_str),
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.edit_message_text(_("❌ Update failed."))
    return True


async def handle_reactivate_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Reactivate' button (`reactivate_<id>`)."""
    if not data.startswith("reactivate_"):
        return False

    product_id = _parse_id(data.replace("reactivate_", ""))
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text(_("❌ Product not found."))
        return True
    await db.reactivate_product(product_id)
    name = (product.get("name") or _("Unknown"))[:50]
    await query.edit_message_text(
        _("▶️ <b>Reactivated:</b> {name}").format(name=_escape_html(name)),
        parse_mode=ParseMode.HTML,
    )
    return True


async def handle_picker(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle inline pickers that need a follow-up text reply (`set*_<id>`)."""
    if data.startswith("settarget_"):
        product_id = _parse_id(data.replace("settarget_", ""))
        if product_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        product = await _get_user_product(context, product_id, user_id)
        if not product:
            await query.edit_message_text(_("❌ Product not found."))
            return True
        name = (product.get("name") or _("Unknown"))[:50]
        current = _safe_dec(product.get("current_price"))
        price_info = _(" (current: €{price:.2f})").format(price=current) if current else ""
        context.user_data["pending_action"] = ("target", product_id)
        await query.edit_message_text(
            _(
                "🎯 <b>{name}</b>{price_info}\n\nType the target price (e.g. <code>29.99</code>):"
            ).format(name=_escape_html(name), price_info=price_info),
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("setsoglia_"):
        product_id = _parse_id(data.replace("setsoglia_", ""))
        if product_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        product = await _get_user_product(context, product_id, user_id)
        if not product:
            await query.edit_message_text(_("❌ Product not found."))
            return True
        name = (product.get("name") or _("Unknown"))[:50]
        context.user_data["pending_action"] = ("threshold", product_id)
        await query.edit_message_text(
            _(
                "🎯 <b>{name}</b>\n\nType the threshold (e.g. <code>20%</code> or <code>50</code>):"
            ).format(name=_escape_html(name)),
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("setrefresh_"):
        product_id = _parse_id(data.replace("setrefresh_", ""))
        if product_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        product = await _get_user_product(context, product_id, user_id)
        if not product:
            await query.edit_message_text(_("❌ Product not found."))
            return True
        name = (product.get("name") or _("Unknown"))[:50]
        context.user_data["pending_action"] = ("refresh", product_id)
        await query.edit_message_text(
            _(
                "🔄 <b>{name}</b>\n\n"
                "Type the interval in minutes (e.g. <code>30</code>, "
                "<code>720</code> for 12h):"
            ).format(name=_escape_html(name)),
            parse_mode=ParseMode.HTML,
        )
        return True

    return False
