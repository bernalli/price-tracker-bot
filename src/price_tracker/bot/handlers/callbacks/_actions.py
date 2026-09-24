"""Per-product action callbacks (`edit_*`, `pause_*`, `remove_*`, `reset_*`,
`reactivate_*`, `set*_*` pickers).

Split out of `handlers/callbacks/_product.py` to keep each module under a
500-line budget.
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

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def handle_edit_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Modifica' button (`edit_<id>`)."""
    if not data.startswith("edit_"):
        return False

    product_id = _parse_id(data.replace("edit_", ""))
    if product_id is None:
        await query.edit_message_text("❌ ID non valido.")
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text("❌ Prodotto non trovato.")
        return True

    name = (product.get("name") or "Sconosciuto")[:60]
    threshold_type = product.get("threshold_type", "percentage")
    threshold_value = product.get("threshold_value", "10")
    threshold_str = _format_threshold(threshold_type, threshold_value)
    target = _safe_dec(product.get("target_price"))
    target_str = f"€{target:.2f}" if target else "non impostato"

    initial = _safe_dec(product.get("initial_price"))
    current = _safe_dec(product.get("current_price"))
    initial_str = f"€{initial:.2f}" if initial else "N/D"

    edit_buttons = [
        [InlineKeyboardButton("🔔 Ogni ribasso", callback_data=f"track_any_{product_id}")],
        [InlineKeyboardButton("📉 Soglia % o €", callback_data=f"track_threshold_{product_id}")],
        [InlineKeyboardButton("💰 Prezzo target", callback_data=f"track_target_{product_id}")],
    ]
    if initial and current and initial != current:
        edit_buttons.append(
            [InlineKeyboardButton("🔄 Azzera prezzo base", callback_data=f"reset_{product_id}")]
        )

    await query.message.reply_text(
        f"✏️ <b>Modifica #{product_id}</b> {_escape_html(name)}\n\n"
        f"🎯 Soglia attuale: <b>{threshold_str}</b>\n"
        f"🏁 Target attuale: <b>{target_str}</b>\n"
        f"📌 Prezzo base: <b>{initial_str}</b>\n\n"
        f"<b>Cosa vuoi modificare?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(edit_buttons),
    )
    return True


async def handle_pause_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Pausa' button (`pause_<id>`)."""
    if not data.startswith("pause_"):
        return False

    product_id = _parse_id(data.replace("pause_", ""))
    if product_id is None:
        await query.edit_message_text("❌ ID non valido.")
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text("❌ Prodotto non trovato.")
        return True

    name = (product.get("name") or "Sconosciuto")[:50]
    await db.deactivate_product(product_id)
    await query.edit_message_text(
        f"⏸ <b>In pausa:</b> {_escape_html(name)}\nUsa /riattiva {product_id} per riattivarlo.",
        parse_mode=ParseMode.HTML,
    )
    return True


async def handle_remove_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Elimina' button (`remove_<id>`) — shows confirmation prompt."""
    if not data.startswith("remove_"):
        return False

    product_id = _parse_id(data.replace("remove_", ""))
    if product_id is None:
        await query.edit_message_text("❌ ID non valido.")
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text("❌ Prodotto non trovato.")
        return True

    name = (product.get("name") or "Sconosciuto")[:50]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑 Sì, elimina tutto",
                    callback_data=f"confirm_delete_{product_id}",
                ),
                InlineKeyboardButton("⏸ Solo pausa", callback_data=f"pause_{product_id}"),
                InlineKeyboardButton("❌ Annulla", callback_data="cancel_delete"),
            ]
        ]
    )
    await query.edit_message_text(
        f"❓ Cosa vuoi fare con <b>{_escape_html(name)}</b>?",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return True


async def handle_reset_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Azzera prezzo base' button (`reset_<id>`)."""
    if not data.startswith("reset_"):
        return False

    product_id = _parse_id(data.replace("reset_", ""))
    if product_id is None:
        await query.edit_message_text("❌ ID non valido.")
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text("❌ Prodotto non trovato.")
        return True
    success = await db.reset_initial_price(product_id)
    if success:
        name = (product.get("name") or "Sconosciuto")[:60]
        current = _safe_dec(product.get("current_price"))
        price_str = f"€{current:.2f}" if current else "N/D"
        await query.edit_message_text(
            f"✅ Prezzo base aggiornato!\n\n"
            f"📦 <b>#{product_id}</b> {_escape_html(name)}\n"
            f"💰 Nuovo base: <b>{price_str}</b>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.edit_message_text("❌ Impossibile aggiornare.")
    return True


async def handle_reactivate_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Riattiva' button (`reactivate_<id>`)."""
    if not data.startswith("reactivate_"):
        return False

    product_id = _parse_id(data.replace("reactivate_", ""))
    if product_id is None:
        await query.edit_message_text("❌ ID non valido.")
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text("❌ Prodotto non trovato.")
        return True
    await db.reactivate_product(product_id)
    name = (product.get("name") or "Sconosciuto")[:50]
    await query.edit_message_text(
        f"▶️ <b>Riattivato:</b> {_escape_html(name)}",
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
            await query.edit_message_text("❌ ID non valido.")
            return True
        product = await _get_user_product(context, product_id, user_id)
        if not product:
            await query.edit_message_text("❌ Prodotto non trovato.")
            return True
        name = (product.get("name") or "Sconosciuto")[:50]
        current = _safe_dec(product.get("current_price"))
        price_info = f" (attuale: €{current:.2f})" if current else ""
        context.user_data["pending_action"] = ("target", product_id)
        await query.edit_message_text(
            f"🎯 <b>{_escape_html(name)}</b>{price_info}\n\n"
            f"Scrivi il prezzo target (es. <code>29.99</code>):",
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("setsoglia_"):
        product_id = _parse_id(data.replace("setsoglia_", ""))
        if product_id is None:
            await query.edit_message_text("❌ ID non valido.")
            return True
        product = await _get_user_product(context, product_id, user_id)
        if not product:
            await query.edit_message_text("❌ Prodotto non trovato.")
            return True
        name = (product.get("name") or "Sconosciuto")[:50]
        context.user_data["pending_action"] = ("threshold", product_id)
        await query.edit_message_text(
            f"🎯 <b>{_escape_html(name)}</b>\n\n"
            f"Scrivi la soglia (es. <code>20%</code> o <code>50</code>):",
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("setrefresh_"):
        product_id = _parse_id(data.replace("setrefresh_", ""))
        if product_id is None:
            await query.edit_message_text("❌ ID non valido.")
            return True
        product = await _get_user_product(context, product_id, user_id)
        if not product:
            await query.edit_message_text("❌ Prodotto non trovato.")
            return True
        name = (product.get("name") or "Sconosciuto")[:50]
        context.user_data["pending_action"] = ("refresh", product_id)
        await query.edit_message_text(
            f"🔄 <b>{_escape_html(name)}</b>\n\n"
            f"Scrivi l'intervallo in minuti (es. <code>30</code>, "
            f"<code>720</code> per 12h):",
            parse_mode=ParseMode.HTML,
        )
        return True

    return False
