"""Product-scoped callback handlers (delete/check/chart/edit/pause/remove/...).

Split out of `handlers/callbacks/__init__.py` to keep the dispatcher under
the 500-LOC budget [Task 17]. Each function takes the `(query, context, db,
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
    _parse_id,
    _safe_dec,
)
from price_tracker.bot.handlers.history import _generate_chart
from price_tracker.bot.keyboards import build_threshold_keyboard
from price_tracker.bot.messages import _

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
        product_id = _parse_id(data.replace("confirm_delete_", ""))
        if product_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        product = await _get_user_product(context, product_id, user_id)
        if product:
            name = product.get("name") or _("Unknown")
            await db.delete_product(product_id, user_id=user_id)
            await query.edit_message_text(
                _("🗑 Permanently deleted: <b>{name}</b>").format(name=_escape_html(name[:80])),
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(_("❌ Product not found or not authorized."))
        return True

    if data == "cancel_delete":
        await query.edit_message_text(_("👍 Operation cancelled."))
        return True

    if data == "delete_all":
        products = await db.get_active_products(user_id)
        count = len(products)
        if count == 0:
            await query.edit_message_text(_("📭 No products to delete."))
            return True

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        _("⚠️ Yes, delete all ({count})").format(count=count),
                        callback_data="confirmdeleteall",
                    ),
                    InlineKeyboardButton(_("❌ Cancel"), callback_data="cancel_delete"),
                ]
            ]
        )
        await query.edit_message_text(
            _(
                "🚨 <b>Warning!</b>\n\n"
                "You are about to <b>permanently delete {count} products</b> "
                "and all their price history.\n\n"
                "This action is <b>not reversible</b>."
            ).format(count=count),
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
        await query.edit_message_text(
            _("🗑 <b>Deleted {count} products</b> and all their history.").format(count=count),
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

    product_id = _parse_id(data.replace("check_", ""))
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text(_("❌ Product not found."))
        return True

    await query.edit_message_text(_("⏳ Checking price..."))
    from price_tracker.core.scraper_base import detect_currency  # noqa: PLC0415

    scheduler = context.bot_data["scheduler"]
    try:
        result = await scheduler.check_one_product_for_user(product_id=product_id, user_id=user_id)
    except Exception as e:  # noqa: BLE001 — surface error to user
        await query.edit_message_text(_("❌ Error: {error}").format(error=e))
        return True
    alert = result.alert

    product = await db.get_product(product_id)
    if product is None:
        await query.edit_message_text(_("❌ Product not found."))
        return True
    name = (product.get("name") or _("Unknown"))[:60]
    current = _safe_dec(product.get("current_price"))
    initial = _safe_dec(product.get("initial_price"))
    p_currency = product.get("currency", "") or detect_currency(product.get("url", "")) or "EUR"
    price_str = _convert_display(current, p_currency) if current else _("N/A")

    text = _("✅ <b>#{pid}</b> {name}\n💰 Price: {price}").format(
        pid=product_id, name=_escape_html(name), price=price_str
    )
    if initial and current and initial > 0 and initial != current:
        diff = (initial - current) / initial * 100
        if diff > 0:
            text += _("\n📌 Initial: €{initial:.2f} (<i>-{diff:.1f}% since tracking</i>)").format(
                initial=initial, diff=diff
            )

    if alert:
        text += _("\n\n🔔 <b>PRICE JUST DROPPED!</b>")
        text += _("\n💸 Was: €{old:.2f} → Now: €{new:.2f}").format(
            old=alert.old_price, new=alert.new_price
        )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(_("📊 Price history"), callback_data=f"chart_{product_id}"),
            ]
        ]
    )
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return True


async def handle_chart_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the per-product 'Price history' button (`chart_<id>`)."""
    if not data.startswith("chart_"):
        return False

    product_id = _parse_id(data.replace("chart_", ""))
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text(_("❌ Product not found."))
        return True

    chart = await _generate_chart(db, product_id, product)
    if chart:
        name = (product.get("name") or _("Product"))[:50]
        await query.message.reply_photo(
            photo=InputFile(chart, filename=f"chart_{product_id}.png"),
            caption=f"📊 <b>#{product_id}</b> {_escape_html(name)}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.message.reply_text(
            _("📭 Not enough data to generate the chart (at least 2 points needed).")
        )
    return True


# msgid strings, translated lazily at call time so the module-level table does
# not freeze the locale that happened to be active at import.
_PREF_PROMPTS: dict[str, tuple[str | None, str | None, str]] = {
    "pref_new_": ("new", None, "🆕 Preference: <b>New only</b>"),
    "pref_used_": ("used", None, "♻️ Preference: <b>Used only</b>"),
    "pref_amazon_": (None, "amazon", "📦 Preference: <b>Sold by Amazon only</b>"),
    "pref_anyseller_": (None, "any", "🏪 Preference: <b>Any seller</b>"),
    "pref_default_": (None, None, "👍 Preference: <b>No filter</b>"),
}


async def handle_amazon_pref(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle Amazon condition/seller preference buttons (`pref_*`)."""
    for prefix, (condition, seller, label) in _PREF_PROMPTS.items():
        if data.startswith(prefix):
            product_id = _parse_id(data.replace(prefix, ""))
            if product_id is None:
                await query.edit_message_text(_("❌ Invalid ID."))
                return True
            await db.set_product_preferences(product_id, condition=condition, seller=seller)
            name = await _get_product_name(db, product_id)
            await query.edit_message_text(
                _("{label} for #{pid}\n📦 {name}\n\n<b>How do you want to be notified?</b>").format(
                    label=_(label), pid=product_id, name=_escape_html(name)
                ),
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
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        await db.set_threshold(product_id, "any_drop", "0")
        name = await _get_product_name(db, product_id)
        await query.edit_message_text(
            _(
                "🔔 <b>Every drop</b> enabled for #{pid}\n"
                "📦 {name}\n\n"
                "You will get a notification on every price drop."
            ).format(pid=product_id, name=_escape_html(name)),
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("track_threshold_"):
        product_id = _parse_id(data.replace("track_threshold_", ""))
        if product_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        name = await _get_product_name(db, product_id)
        context.user_data["pending_action"] = ("threshold", product_id)
        await query.edit_message_text(
            _(
                "📉 <b>Set threshold for #{pid}</b>\n"
                "📦 {name}\n\n"
                "Type the threshold you want:\n"
                "• <code>20%</code> — alert me if it drops by 20%\n"
                "• <code>50</code> — alert me if it drops by €50"
            ).format(pid=product_id, name=_escape_html(name)),
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("track_target_"):
        product_id = _parse_id(data.replace("track_target_", ""))
        if product_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        name = await _get_product_name(db, product_id)
        product = await db.get_product(product_id)
        current = _safe_dec(product.get("current_price")) if product else None
        currency = product.get("currency", "EUR") if product else "EUR"
        price_hint = (
            _("\n💰 Current price: {price}").format(price=_convert_display(current, currency))
            if current
            else ""
        )
        context.user_data["pending_action"] = ("target", product_id)
        await query.edit_message_text(
            _(
                "💰 <b>Set target price for #{pid}</b>\n"
                "📦 {name}{hint}\n\n"
                "Type the price you are aiming for (e.g. <code>100</code>):"
            ).format(pid=product_id, name=_escape_html(name), hint=price_hint),
            parse_mode=ParseMode.HTML,
        )
        return True

    if data.startswith("track_default_"):
        product_id = _parse_id(data.replace("track_default_", ""))
        if product_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        product = await _get_user_product(context, product_id, user_id)
        if not product:
            await query.edit_message_text(_("❌ Product not found."))
            return True
        await db.set_threshold(product_id, "percentage", "10")
        name = (product.get("name") or _("Unknown"))[:60]
        await query.edit_message_text(
            _(
                "👍 <b>Default threshold -10%</b> for #{pid}\n"
                "📦 {name}\n\n"
                "You will get a notification when the price drops 10% "
                "from the initial price."
            ).format(pid=product_id, name=_escape_html(name)),
            parse_mode=ParseMode.HTML,
        )
        return True

    return False


# Per-product action callbacks (edit/pause/remove/reset/reactivate/pickers)
# live in `_actions.py` to keep this module under the 500-LOC budget.
