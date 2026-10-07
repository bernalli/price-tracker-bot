"""Product-scoped callback handlers (delete/check/chart/edit/pause/remove/...).

Split out of `handlers/callbacks/__init__.py` to keep the dispatcher under
a 500-line budget. Each function takes the `(query, context, db,
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

from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.handlers._cards import _currency
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _get_user_product,
    _parse_id,
    out_of_stock_line,
)
from price_tracker.bot.handlers.callbacks._nav import show_card, show_list
from price_tracker.bot.handlers.history import _generate_chart
from price_tracker.bot.keyboards import build_threshold_keyboard
from price_tracker.bot.messages import _, current_locale, ngettext
from price_tracker.bot.ui.width import truncate_to_width
from price_tracker.core.alert import _why
from price_tracker.i18n.format import money

if TYPE_CHECKING:
    from decimal import Decimal

    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def handle_delete_flow(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the delete confirmation flow (`confirm_delete_*`, `cancel_delete`,
    `delete_all`, `confirmdeleteall`).
    """
    if data.startswith("confirm_delete_"):
        product_id = _parse_id(data.replace("confirm_delete_", ""))
        if product_id is None:
            await query.edit_message_text("❌ ID non valido.")
            return True
        product = await _get_user_product(context, product_id, user_id)
        # An admin may delete any product (as with every other product action),
        # so the delete is scoped to the owner, and only a deleted row is confirmed.
        if product and await db.delete_product(product_id, user_id=product["user_id"]):
            name = product.get("name") or _("Product #{product_id}").format(product_id=product_id)
            notice = _("🗑 Deleted: {name}").format(name=_escape_html(truncate_to_width(name, 60)))
        else:
            notice = _("Product not found.")
        await show_list(query, context, db, user_id, notice=notice)
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
                    InlineKeyboardButton(
                        "❌ Annulla", callback_data=encode(Action("list.page", ("a", 1)))
                    ),
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
            await db.delete_product(p["id"], user_id=user_id)
            count += 1
        notice = ngettext(
            "🗑 Deleted {n} product and its price history.",
            "🗑 Deleted {n} products and their price history.",
            count,
        ).format(n=count)
        await show_list(query, context, db, user_id, notice=notice)
        return True

    return False


def _price_or_dash(amount: Decimal, currency: str, loc: str) -> str:
    """``amount`` as money, or the card's dash for a value money() refuses."""
    try:
        return money(amount, currency, locale=loc)
    except ValueError:
        return "—"


async def handle_check_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the per-product 'Check now' button (`check_<id>`): the card shows the outcome."""
    if not data.startswith("check_"):
        return False

    product_id = _parse_id(data.replace("check_", ""))
    if product_id is None:
        await query.edit_message_text("❌ ID non valido.")
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await show_list(query, context, db, user_id, notice=_("Product not found."))
        return True
    if not product.get("is_active"):
        # The scheduler skips an inactive product: nothing would be checked.
        notice = _("⏸ Not checked: tracking is paused. Reactivate it first.")
        await show_card(query, context, product, notice=notice)
        return True

    await query.edit_message_text(_("🔄 Checking..."))
    scheduler = context.bot_data["scheduler"]
    result = None
    try:
        result = await scheduler.check_one_product_for_user(product_id=product_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — the card says the check failed
        logger.warning("Check now failed for product %s: %s", product_id, exc, exc_info=True)

    record = await db.get_product(product_id)
    if record is None:
        await show_list(query, context, db, user_id, notice=_("Product not found."))
        return True
    if result is None:
        notice = _("❌ Could not check this product. Try again later.")
    elif result.alert is not None:
        currency = _currency(record.get("currency"))
        loc = current_locale()
        notice = _("🔔 Price dropped: {old} → {new}").format(
            old=_price_or_dash(result.alert.old_price, currency, loc),
            new=_price_or_dash(result.alert.new_price, currency, loc),
        )
    elif result.reason == "out_of_stock":
        notice = out_of_stock_line()
    elif result.reason is not None:
        why = _why(result.reason, record.get("last_error"))
        notice = _("❌ Not updated: {why}").format(why=why)
    else:
        notice = _("✅ Checked: no significant change.")
    await show_card(query, context, record, notice=notice)
    return True


async def handle_chart_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the per-product 'Storico prezzo' button (`chart_<id>`)."""
    if not data.startswith("chart_"):
        return False

    product_id = _parse_id(data.replace("chart_", ""))
    if product_id is None:
        await query.edit_message_text("❌ ID non valido.")
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text("❌ Prodotto non trovato.")
        return True

    chart = await _generate_chart(db, product_id, product)
    if chart:
        name = truncate_to_width(product.get("name") or "Prodotto", 50)
        await query.message.reply_photo(
            photo=InputFile(chart, filename=f"chart_{product_id}.png"),
            caption=f"📈 <b>#{product_id}</b> {_escape_html(name)}",
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
            product_id = _parse_id(data.replace(prefix, ""))
            if product_id is None:
                await query.edit_message_text("❌ ID non valido.")
                return True
            product = await _get_user_product(context, product_id, user_id)
            if not product:
                await query.edit_message_text("❌ Prodotto non trovato.")
                return True
            await db.set_product_preferences(product_id, condition=condition, seller=seller)
            name = truncate_to_width(product.get("name") or "Sconosciuto", 60)
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
        product_id = _parse_id(data.replace("track_any_", ""))
        if product_id is None:
            await query.edit_message_text("❌ ID non valido.")
            return True
        product = await _get_user_product(context, product_id, user_id)
        if not product:
            await query.edit_message_text("❌ Prodotto non trovato.")
            return True
        await db.set_threshold(product_id, "any_drop", "0")
        name = truncate_to_width(product.get("name") or "Sconosciuto", 60)
        await query.edit_message_text(
            f"🔔 <b>Ogni ribasso</b> attivato per #{product_id}\n"
            f"📦 {_escape_html(name)}\n\n"
            f"Riceverai una notifica ad ogni calo di prezzo.",
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("track_default_"):
        product_id = _parse_id(data.replace("track_default_", ""))
        if product_id is None:
            await query.edit_message_text("❌ ID non valido.")
            return True
        product = await _get_user_product(context, product_id, user_id)
        if not product:
            await query.edit_message_text("❌ Prodotto non trovato.")
            return True
        await db.set_threshold(product_id, "percentage", "10")
        name = truncate_to_width(product.get("name") or "Sconosciuto", 60)
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
