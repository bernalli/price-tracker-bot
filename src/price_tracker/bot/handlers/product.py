"""Product CRUD handlers + URL paste intake.

Ported from monolithic bot.py [item 17]. CSV export/import lives in
`handlers/product_io.py` to keep this file under the 500-LOC budget.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from price_tracker.bot.decorators import (
    _client,
    _convert_display,
    _db,
    _scraper,
    restricted,
)
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_threshold,
    _get_user_product,
    _parse_id,
    _parse_threshold_input,
    _safe_dec,
)
from price_tracker.bot.keyboards import build_threshold_keyboard
from price_tracker.bot.messages import _

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


# Re-exported for callback / pending-action consumers:
_build_threshold_keyboard = build_threshold_keyboard


async def _product_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    label: str,
    callback_prefix: str | None = None,
) -> bool:
    """Show inline product picker if no args. Returns True if picker was shown."""
    db = _db(context)
    user_id = update.effective_user.id
    products = await db.get_active_products(user_id)
    if not products:
        await update.message.reply_text(_("📭 Non hai prodotti tracciati."))
        return True

    buttons = []
    for p in products:
        name = (p.get("name") or "Sconosciuto")[:35]
        current = _safe_dec(p.get("current_price"))
        price_tag = f" €{current:.2f}" if current else ""
        prefix = callback_prefix or action
        buttons.append(
            [
                InlineKeyboardButton(
                    f"#{p['id']} {name}{price_tag}",
                    callback_data=f"{prefix}_{p['id']}",
                )
            ]
        )

    await update.message.reply_text(
        f"📦 <b>{label}:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return True


@restricted
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track a new product. Usage: /add <url>"""
    if not context.args:
        await update.message.reply_text(
            "❌ Uso: /add &lt;url&gt;\n\n"
            "Esempio:\n"
            "<code>/add https://www.amazon.it/dp/B09V3K...</code>\n\n"
            "Oppure incolla direttamente il link in chat!",
            parse_mode=ParseMode.HTML,
        )
        return

    url = context.args[0]
    await _add_product(update, context, url)


@restricted
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a tracked product (with confirmation)."""
    if not context.args:
        db = _db(context)
        user_id = update.effective_user.id
        products = await db.get_active_products(user_id)
        if not products:
            await update.message.reply_text(_("📭 Non hai prodotti tracciati."))
            return

        buttons = []
        for p in products:
            name = (p.get("name") or "Sconosciuto")[:35]
            price = _safe_dec(p.get("current_price"))
            label = f"#{p['id']} {name}"
            if price:
                label += f" €{price:.2f}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"remove_{p['id']}")])

        if len(products) > 1:
            buttons.append(
                [InlineKeyboardButton("🗑 Elimina tutti i prodotti", callback_data="delete_all")]
            )

        await update.message.reply_text(
            "📦 <b>Scegli prodotto da eliminare:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ ID non valido."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Prodotto non trovato."))
        return

    name = product.get("name") or "Sconosciuto"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🗑 Sì, elimina", callback_data=f"confirm_delete_{product_id}"),
                InlineKeyboardButton("❌ Annulla", callback_data="cancel_delete"),
            ]
        ]
    )
    await update.message.reply_text(
        f"⚠️ Vuoi eliminare <b>definitivamente</b> questo prodotto?\n\n"
        f"📦 #{product_id} — {_escape_html(name[:80])}\n\n"
        f"Verrà cancellato anche tutto lo storico prezzi.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@restricted
async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or clear the target price for a product."""
    if not context.args:
        await _product_picker(
            update,
            context,
            "target",
            "Scegli prodotto per impostare target",
            "settarget",
        )
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Uso: /target &lt;id&gt; &lt;prezzo&gt;\n"
            "Esempio: <code>/target 3 29.99</code>\n\n"
            "Usa <code>/target &lt;id&gt; 0</code> per rimuovere il target.",
            parse_mode=ParseMode.HTML,
        )
        return

    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ ID non valido."))
        return
    try:
        target = Decimal(context.args[1].replace(",", ".").replace("€", ""))
    except (InvalidOperation, ValueError):
        await update.message.reply_text(_("❌ Prezzo non valido."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Prodotto non trovato."))
        return

    db = _db(context)
    if target <= 0:
        await db.set_target_price(product_id, None)
        await update.message.reply_text(f"🎯 Target rimosso per #{product_id}.")
        return

    await db.set_target_price(product_id, target)
    name = product.get("name") or "Sconosciuto"
    current = _safe_dec(product.get("current_price"))
    currency = product.get("currency", "EUR")
    target_display = _convert_display(target, currency)
    lines = [
        f"🎯 Target impostato: <b>{target_display}</b>",
        f"📦 {_escape_html(name[:80])}",
    ]
    if current:
        current_display = _convert_display(current, currency)
        if current <= target:
            lines.append(f"💰 Attuale: {current_display} — <b>già raggiunto!</b>")
        else:
            diff_pct = ((current - target) / current) * 100
            lines.append(f"💰 Attuale: {current_display} (-{diff_pct:.1f}% necessario)")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the price-drop threshold for a product."""
    if not context.args:
        await _product_picker(
            update,
            context,
            "threshold",
            "Scegli prodotto per impostare soglia",
            "setsoglia",
        )
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Uso: /soglia &lt;id&gt; &lt;valore&gt;\n\n"
            "Esempi:\n"
            "<code>/soglia 3 20%</code> — avvisami se scende del 20%\n"
            "<code>/soglia 3 50</code> — avvisami se scende di €50\n"
            "<code>/soglia 3 ogni</code> — avvisami ad ogni ribasso",
            parse_mode=ParseMode.HTML,
        )
        return

    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ ID non valido."))
        return
    try:
        threshold_type, threshold_value = _parse_threshold_input(context.args[1])
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Prodotto non trovato."))
        return

    await _db(context).set_threshold(product_id, threshold_type, threshold_value)
    name = product.get("name") or "Sconosciuto"
    threshold_str = _format_threshold(threshold_type, threshold_value)
    await update.message.reply_text(
        f"🎯 Soglia impostata: <b>{threshold_str}</b>\n"
        f"📦 #{product_id} — {_escape_html(name[:80])}",
        parse_mode=ParseMode.HTML,
    )


# ── Shared add product logic ─────────────────────────────────────


async def _add_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
) -> None:
    """Track a brand-new product or reactivate a paused duplicate."""
    db = _db(context)
    client = _client(context)
    scraper = _scraper(context)
    user_id = update.effective_user.id

    # Deferred import: legacy `scrapers.identify_site` lives outside the package.
    from scrapers import identify_site  # noqa: PLC0415

    from price_tracker.core.scraper_base import detect_currency  # noqa: PLC0415

    # Check for duplicates PER USER
    existing = await db.get_product_by_url_for_user(url, user_id)
    if existing:
        is_active = existing.get("is_active", 0)
        if is_active:
            current = _safe_dec(existing.get("current_price"))
            price_str = f"\n💰 Prezzo attuale: €{current:.2f}" if current else ""
            await update.message.reply_text(
                f"ℹ️ Stai già tracciando questo prodotto (#{existing['id']}).{price_str}"
            )
            return
        await db.reactivate_product(existing["id"])
        await update.message.reply_text(f"♻️ Prodotto riattivato! (#{existing['id']})")
        return

    msg = await update.message.reply_text(_("🔍 Analizzo il prodotto..."))
    domain = identify_site(url)
    result = await scraper.scrape(url, client)

    if result.price is None:
        error_msg = result.error or "Prezzo non trovato"
        await msg.edit_text(
            f"❌ Non sono riuscito a trovare il prezzo.\n"
            f"Motivo: {error_msg}\n\n"
            f"💡 Prova a verificare che il link sia corretto "
            f"e il prodotto disponibile."
        )
        return

    currency = result.currency or detect_currency(url) or "EUR"

    product_id = await db.add_product(
        user_id=user_id,
        url=url,
        name=result.name,
        domain=domain,
        price=result.price,
        target_price=None,
        threshold_type="percentage",
        threshold_value="10",
        currency=currency,
    )

    name = result.name or "Prodotto"
    name_short = name[:80] + ("..." if len(name) > 80 else "")

    lines = [
        f"✅ <b>Prodotto aggiunto!</b> (#{product_id})",
        "",
        f"📦 {_escape_html(name_short)}",
        f"💰 Prezzo: <b>{_convert_display(result.price, currency)}</b>",
        f"🌐 Sito: {domain}",
    ]

    if domain and "amazon" in domain.lower():
        # Show Amazon preferences menu first
        lines.append("\n📋 <b>Preferenze Amazon:</b>")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🆕 Solo Nuovo", callback_data=f"pref_new_{product_id}"),
                    InlineKeyboardButton("♻️ Solo Usato", callback_data=f"pref_used_{product_id}"),
                ],
                [
                    InlineKeyboardButton(
                        "📦 Solo Amazon", callback_data=f"pref_amazon_{product_id}"
                    ),
                    InlineKeyboardButton(
                        "🏪 Qualsiasi venditore",
                        callback_data=f"pref_anyseller_{product_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "👍 Va bene tutto (default)",
                        callback_data=f"pref_default_{product_id}",
                    ),
                ],
            ]
        )
    else:
        # Non-Amazon: show threshold menu directly
        lines.append("\n<b>Come vuoi essere avvisato?</b>")
        keyboard = build_threshold_keyboard(product_id)

    await msg.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


def register(app: Application) -> None:
    """Register product CRUD command handlers on `app`.

    URL/text intake handlers live in `handlers.text_input` — they are
    registered separately by the aggregator (`handlers/__init__.py`).
    """
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("aggiungi", cmd_add))
    app.add_handler(CommandHandler("elimina", cmd_delete))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("target", cmd_target))
    app.add_handler(CommandHandler("soglia", cmd_threshold))
    app.add_handler(CommandHandler("threshold", cmd_threshold))
