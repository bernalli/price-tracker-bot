"""Product-scoped callback handlers (delete/check/chart/edit/pause/remove/...).

Split out of `handlers/callbacks/__init__.py` to keep the dispatcher under
the 500-LOC budget [item 17]. Each function takes the `(query, context, db,
user_id, data)` tuple and returns `True` if it handled the callback, `False`
otherwise — keeps the dispatcher a thin if/elif on prefixes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode

from price_tracker.bot.decorators import (
    _convert_display,
)
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _get_product_name,
    _get_user_product,
    _safe_dec,
)
from price_tracker.bot.handlers.history import _generate_chart
from price_tracker.bot.keyboards import build_threshold_keyboard

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def handle_delete_flow(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the delete confirmation flow (`confirm_delete_*`, `cancel_delete`,
    `delete_all`, `confirmdeleteall`).
    """
    if data.startswith("confirm_delete_"):
        product_id = int(data.replace("confirm_delete_", ""))
        product = await _get_user_product(context, product_id, user_id)
        if product:
            name = product.get("name") or "Sconosciuto"
            await db.delete_product(product_id)
            await query.edit_message_text(
                f"🗑 Eliminato definitivamente: <b>{_escape_html(name[:80])}</b>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text("❌ Prodotto non trovato o non autorizzato.")
        return True

    if data == "cancel_delete":
        await query.edit_message_text("👍 Operazione annullata.")
        return True

    if data == "delete_all":
        products = await db.get_active_products(user_id)
        count = len(products)
        if count == 0:
            await query.edit_message_text("📭 Nessun prodotto da eliminare.")
            return True

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"⚠️ Sì, elimina tutti ({count})",
                        callback_data="confirmdeleteall",
                    ),
                    InlineKeyboardButton("❌ Annulla", callback_data="cancel_delete"),
                ]
            ]
        )
        await query.edit_message_text(
            f"🚨 <b>Attenzione!</b>\n\n"
            f"Stai per eliminare <b>definitivamente {count} prodotti</b> "
            f"e tutto il loro storico prezzi.\n\n"
            f"Questa azione <b>non è reversibile</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return True

    if data == "confirmdeleteall":
        products = await db.get_active_products(user_id)
        count = 0
        for p in products:
            await db.delete_product(p["id"])
            count += 1
        await query.edit_message_text(
            f"🗑 <b>Eliminati {count} prodotti</b> e tutto il loro storico.",
            parse_mode=ParseMode.HTML,
        )
        return True

    return False


async def handle_check_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the per-product 'Check now' button (`check_<id>`)."""
    if not data.startswith("check_"):
        return False

    product_id = int(data.replace("check_", ""))
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text("❌ Prodotto non trovato.")
        return True

    await query.edit_message_text("⏳ Controllo prezzo in corso...")
    from price_tracker.core.scraper_base import detect_currency  # noqa: PLC0415

    scheduler = context.bot_data["scheduler"]
    try:
        result = await scheduler.check_one_product_for_user(product_id=product_id, user_id=user_id)
    except Exception as e:  # noqa: BLE001 — surface error to user
        await query.edit_message_text(f"❌ Errore: {e}")
        return True
    alert = result.alert

    product = await db.get_product(product_id)
    if product is None:
        await query.edit_message_text("❌ Prodotto non trovato.")
        return True
    name = (product.get("name") or "Sconosciuto")[:60]
    current = _safe_dec(product.get("current_price"))
    initial = _safe_dec(product.get("initial_price"))
    p_currency = product.get("currency", "") or detect_currency(product.get("url", "")) or "EUR"
    price_str = _convert_display(current, p_currency) if current else "N/D"

    text = f"✅ <b>#{product_id}</b> {_escape_html(name)}\n💰 Prezzo: {price_str}"
    if initial and current and initial > 0 and initial != current:
        diff = (initial - current) / initial * 100
        if diff > 0:
            text += f"\n📌 Iniziale: €{initial:.2f} (<i>-{diff:.1f}% dal tracking</i>)"

    if alert:
        text += "\n\n🔔 <b>PREZZO APPENA SCESO!</b>"
        text += f"\n💸 Era: €{alert.old_price:.2f} → Ora: €{alert.new_price:.2f}"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Storico prezzo", callback_data=f"chart_{product_id}"),
            ]
        ]
    )
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return True


async def handle_chart_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the per-product 'Storico prezzo' button (`chart_<id>`)."""
    if not data.startswith("chart_"):
        return False

    product_id = int(data.replace("chart_", ""))
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text("❌ Prodotto non trovato.")
        return True

    chart = await _generate_chart(db, product_id, product)
    if chart:
        name = (product.get("name") or "Prodotto")[:50]
        await query.message.reply_photo(
            photo=InputFile(chart, filename=f"chart_{product_id}.png"),
            caption=f"📊 <b>#{product_id}</b> {_escape_html(name)}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.message.reply_text(
            "📭 Dati insufficienti per generare il grafico (servono almeno 2 punti)."
        )
    return True


_PREF_PROMPTS: dict[str, tuple[str | None, str | None, str]] = {
    "pref_new_": ("new", None, "🆕 Preferenza: <b>Solo Nuovo</b>"),
    "pref_used_": ("used", None, "♻️ Preferenza: <b>Solo Usato</b>"),
    "pref_amazon_": (None, "amazon", "📦 Preferenza: <b>Solo venduto da Amazon</b>"),
    "pref_anyseller_": (None, "any", "🏪 Preferenza: <b>Qualsiasi venditore</b>"),
    "pref_default_": (None, None, "👍 Preferenza: <b>Nessun filtro</b>"),
}


async def handle_amazon_pref(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle Amazon condition/seller preference buttons (`pref_*`)."""
    for prefix, (condition, seller, label) in _PREF_PROMPTS.items():
        if data.startswith(prefix):
            product_id = int(data.replace(prefix, ""))
            await db.set_product_preferences(product_id, condition=condition, seller=seller)
            name = await _get_product_name(db, product_id)
            await query.edit_message_text(
                f"{label} per #{product_id}\n"
                f"📦 {_escape_html(name)}\n\n"
                f"<b>Come vuoi essere avvisato?</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=build_threshold_keyboard(product_id),
            )
            return True
    return False


async def handle_track_choice(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle tracking-mode choice buttons (`track_*`)."""
    if data.startswith("track_any_"):
        product_id = int(data.replace("track_any_", ""))
        await db.set_threshold(product_id, "any_drop", "0")
        name = await _get_product_name(db, product_id)
        await query.edit_message_text(
            f"🔔 <b>Ogni ribasso</b> attivato per #{product_id}\n"
            f"📦 {_escape_html(name)}\n\n"
            f"Riceverai una notifica ad ogni calo di prezzo.",
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("track_threshold_"):
        product_id = int(data.replace("track_threshold_", ""))
        name = await _get_product_name(db, product_id)
        context.user_data["pending_action"] = ("threshold", product_id)
        await query.edit_message_text(
            f"📉 <b>Imposta soglia per #{product_id}</b>\n"
            f"📦 {_escape_html(name)}\n\n"
            f"Scrivi la soglia desiderata:\n"
            f"• <code>20%</code> — avvisami se scende del 20%\n"
            f"• <code>50</code> — avvisami se scende di €50",
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("track_target_"):
        product_id = int(data.replace("track_target_", ""))
        name = await _get_product_name(db, product_id)
        product = await db.get_product(product_id)
        current = _safe_dec(product.get("current_price")) if product else None
        currency = product.get("currency", "EUR") if product else "EUR"
        price_hint = (
            f"\n💰 Prezzo attuale: {_convert_display(current, currency)}" if current else ""
        )
        context.user_data["pending_action"] = ("target", product_id)
        await query.edit_message_text(
            f"💰 <b>Imposta prezzo target per #{product_id}</b>\n"
            f"📦 {_escape_html(name)}{price_hint}\n\n"
            f"Scrivi il prezzo obiettivo (es. <code>100</code>):",
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("track_default_"):
        product_id = int(data.replace("track_default_", ""))
        name = await _get_product_name(db, product_id)
        await query.edit_message_text(
            f"👍 <b>Soglia default -10%</b> per #{product_id}\n"
            f"📦 {_escape_html(name)}\n\n"
            f"Riceverai una notifica quando il prezzo scende del 10% "
            f"dal prezzo iniziale.",
            parse_mode=ParseMode.HTML,
        )
        return True

    return False


# Per-product action callbacks (edit/pause/remove/reset/reactivate/pickers)
# live in `_actions.py` to keep this module under the 500-LOC budget.
