"""Product CRUD handlers + URL paste intake.

Split out of the original monolithic bot.py module. CSV export/import lives in
`handlers/product_io.py` to keep this file under the 500-line budget.
"""

from __future__ import annotations

import asyncio
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

from price_tracker.app.inputs import (
    SCALAR_MAX_CHARS,
    Absolute,
    AnyDrop,
    Percentage,
    parse_threshold,
)
from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.decorators import (
    _client,
    _convert_display,
    _db,
    _scraper,
    restricted,
    with_locale,
)
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_threshold,
    _get_user_product,
    _parse_id,
    _safe_dec,
)
from price_tracker.bot.keyboards import build_threshold_keyboard
from price_tracker.bot.messages import _
from price_tracker.bot.ui.width import truncate_to_width
from price_tracker.core.textlimits import truncate_visible

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


# Re-exported for callback / pending-action consumers:
_build_threshold_keyboard = build_threshold_keyboard


async def _product_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    prompt: str,
    callback_prefix: str | None = None,
) -> bool:
    """Show inline product picker if no args. Returns True if picker was shown."""
    db = _db(context)
    user_id = update.effective_user.id
    products = await db.get_active_products(user_id)
    if not products:
        await update.message.reply_text(_("📭 You have no tracked products."))
        return True

    buttons = []
    for p in products:
        name = truncate_to_width(p.get("name") or _("Unknown"), 35)
        current = _safe_dec(p.get("current_price"))
        price_tag = f" €{current:.2f}" if current else ""
        action_name = {
            "settarget": "product.target",
            "setsoglia": "product.threshold",
        }[callback_prefix or action]
        buttons.append(
            [
                InlineKeyboardButton(
                    f"#{p['id']} {name}{price_tag}",
                    callback_data=encode(Action(action_name, (p["id"],))),
                )
            ]
        )

    await update.message.reply_text(
        prompt,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return True


@with_locale
@restricted
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track a new product. Usage: /add <url>"""
    if not context.args:
        await update.message.reply_text(
            _(
                "❌ Usage: /add &lt;url&gt;\n\n"
                "Example:\n"
                "<code>/add https://www.amazon.it/dp/B09V3K...</code>\n\n"
                "Or paste the link directly into the chat!"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    url = context.args[0]
    await _add_product(update, context, url)


@with_locale
@restricted
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a tracked product (with confirmation)."""
    if not context.args:
        db = _db(context)
        user_id = update.effective_user.id
        products = await db.get_active_products(user_id)
        if not products:
            await update.message.reply_text(_("📭 You have no tracked products."))
            return

        buttons = []
        for p in products:
            name = truncate_to_width(p.get("name") or _("Unknown"), 35)
            price = _safe_dec(p.get("current_price"))
            label = f"#{p['id']} {name}"
            if price:
                label += f" €{price:.2f}"
            buttons.append(
                [
                    InlineKeyboardButton(
                        label, callback_data=encode(Action("product.remove", (p["id"],)))
                    )
                ]
            )

        if len(products) > 1:
            buttons.append(
                [
                    InlineKeyboardButton(
                        _("🗑 Delete all products"), callback_data=encode(Action("list.remove_all"))
                    )
                ]
            )

        await update.message.reply_text(
            _("📦 <b>Choose a product to delete:</b>"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ Invalid ID."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Product not found."))
        return

    name = product.get("name") or _("Unknown")
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _("🗑 Yes, delete"),
                    callback_data=encode(Action("product.remove_ok", (product_id,))),
                ),
                InlineKeyboardButton(_("❌ Cancel"), callback_data=encode(Action("delete.cancel"))),
            ]
        ]
    )
    await update.message.reply_text(
        _(
            "⚠️ Do you want to <b>permanently</b> delete this product?\n\n"
            "📦 #{product_id} — {name}\n\n"
            "Its entire price history will also be deleted."
        ).format(
            product_id=product_id,
            name=_escape_html(truncate_to_width(name, 80)),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@with_locale
@restricted
async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or clear the target price for a product."""
    if not context.args:
        await _product_picker(
            update,
            context,
            "target",
            _("📦 <b>Choose a product to set a target:</b>"),
            "settarget",
        )
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            _(
                "❌ Usage: /target &lt;id&gt; &lt;price&gt;\n"
                "Example: <code>/target 3 29.99</code>\n\n"
                "Use <code>/target &lt;id&gt; 0</code> to remove the target."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ Invalid ID."))
        return
    try:
        target = Decimal(context.args[1].replace(",", ".").replace("€", ""))
    except (InvalidOperation, ValueError):
        await update.message.reply_text(_("❌ Invalid price."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Product not found."))
        return

    db = _db(context)
    if target <= 0:
        await db.set_target_price(product_id, None)
        await update.message.reply_text(
            _("🎯 Target removed for #{product_id}.").format(product_id=product_id)
        )
        return

    await db.set_target_price(product_id, target)
    name = product.get("name") or _("Unknown")
    current = _safe_dec(product.get("current_price"))
    currency = product.get("currency", "EUR")
    target_display = _convert_display(target, currency)
    lines = [
        _("🎯 Target set: <b>{target}</b>").format(target=target_display),
        f"📦 {_escape_html(truncate_to_width(name, 80))}",
    ]
    if current:
        current_display = _convert_display(current, currency)
        if current <= target:
            lines.append(
                _("💰 Current: {current} — <b>already reached!</b>").format(current=current_display)
            )
        else:
            diff_pct = ((current - target) / current) * 100
            lines.append(
                _("💰 Current: {current} (-{difference:.1f}% needed)").format(
                    current=current_display, difference=diff_pct
                )
            )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@with_locale
@restricted
async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the price-drop threshold for a product."""
    if not context.args:
        await _product_picker(
            update,
            context,
            "threshold",
            _("📦 <b>Choose a product to set a threshold:</b>"),
            "setsoglia",
        )
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            _(
                "❌ Usage: /threshold &lt;id&gt; &lt;value&gt;\n\n"
                "Examples:\n"
                "<code>/threshold 3 20%</code> — notify me if it drops by 20%\n"
                "<code>/threshold 3 50</code> — notify me if it drops by €50\n"
                "<code>/threshold 3 any</code> — notify me on every drop"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ Invalid ID."))
        return
    parsed = parse_threshold(context.args[1])
    if isinstance(parsed, Percentage):
        threshold_type, threshold_value = "percentage", str(parsed.value)
    elif isinstance(parsed, Absolute):
        threshold_type, threshold_value = "absolute", str(parsed.amount)
    elif isinstance(parsed, AnyDrop):
        threshold_type, threshold_value = "any_drop", "0"
    else:
        rejected = truncate_visible(context.args[1], SCALAR_MAX_CHARS)
        await update.message.reply_text(_("❌ Invalid value: {value}").format(value=rejected))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Product not found."))
        return

    await _db(context).set_threshold(product_id, threshold_type, threshold_value)
    name = product.get("name") or _("Unknown")
    threshold_str = _format_threshold(threshold_type, threshold_value)
    await update.message.reply_text(
        _("🎯 Threshold set: <b>{threshold}</b>\n📦 #{product_id} — {name}").format(
            threshold=threshold_str,
            product_id=product_id,
            name=_escape_html(truncate_to_width(name, 80)),
        ),
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

    from price_tracker.core.scraper_base import detect_currency  # noqa: PLC0415
    from price_tracker.core.url_utils import (  # noqa: PLC0415
        UnsafeURLError,
        UnsupportedSchemeError,
        extract_etld_plus_one,
        validate_public_url,
    )

    # SSRF guard: reject URLs pointing to private/internal/loopback addresses
    # before the URL is stored or fetched. Runs in a thread (getaddrinfo blocks).
    try:
        await asyncio.to_thread(validate_public_url, url)
    except UnsupportedSchemeError as e:
        logger.warning("Rejected non-http(s) product URL from user %d: %s", user_id, e)
        await update.message.reply_text(
            _("❌ URL not allowed: only http:// and https:// links are supported.")
        )
        return
    except UnsafeURLError as e:
        logger.warning("Rejected unsafe product URL from user %d: %s", user_id, e)
        await update.message.reply_text(
            _("❌ URL not allowed: it points to a private or internal address.")
        )
        return

    # Check for duplicates PER USER
    existing = await db.get_product_by_url_for_user(url, user_id)
    if existing:
        is_active = existing.get("is_active", 0)
        if is_active:
            current = _safe_dec(existing.get("current_price"))
            if current:
                duplicate_text = _(
                    "ℹ️ You are already tracking this product (#{product_id}).\n"
                    "💰 Current price: €{current:.2f}"
                ).format(product_id=existing["id"], current=current)
            else:
                duplicate_text = _(
                    "ℹ️ You are already tracking this product (#{product_id})."
                ).format(product_id=existing["id"])
            await update.message.reply_text(duplicate_text)
            return
        await db.reactivate_product(existing["id"])
        await update.message.reply_text(
            _("♻️ Product reactivated! (#{product_id})").format(product_id=existing["id"])
        )
        return

    msg = await update.message.reply_text(_("🔍 Analyzing the product..."))
    domain = extract_etld_plus_one(url)
    scraper_for_url = scraper.resolve(url)
    if scraper_for_url is None:
        await msg.edit_text(
            _(
                "❌ No known scraper for this domain.\n\n"
                "💡 Check that the link is correct or report the unsupported site."
            )
        )
        return
    from price_tracker.core.exceptions import BlockEvent  # noqa: PLC0415

    try:
        result = await scraper_for_url.scrape(url, client)
    except BlockEvent:
        logger.info("Add blocked by site protection for %s", url[:60])
        await msg.edit_text(
            _(
                "🛡 The site is currently blocking automated requests.\n"
                "Please try again in a few minutes."
            )
        )
        return

    if result.price is None:
        error_msg = result.error or _("Price not found")
        await msg.edit_text(
            _(
                "❌ I could not find the price.\n"
                "Reason: {reason}\n\n"
                "💡 Check that the link is correct and the product is available."
            ).format(reason=error_msg)
        )
        return

    currency = result.currency or detect_currency(url) or "EUR"

    product_id = await db.add_product(
        user_id=user_id,
        url=url,
        name=result.name,
        domain=domain,
        initial_price=result.price,
        threshold_type="percentage",
        threshold_value=Decimal("10"),
        currency=currency,
    )

    name = result.name or _("Product")
    name_short = truncate_to_width(name, 80)

    lines = [
        _("✅ <b>Product added!</b> (#{product_id})").format(product_id=product_id),
        "",
        f"📦 {_escape_html(name_short)}",
        _("💰 Price: <b>{price}</b>").format(price=_convert_display(result.price, currency)),
        _("🌐 Site: {domain}").format(domain=domain),
    ]

    if domain and "amazon" in domain.lower():
        # Show Amazon preferences menu first
        lines.append(_("\n📋 <b>Amazon preferences:</b>"))
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        _("🆕 New only"),
                        callback_data=encode(Action("product.offer_filter", (product_id, "n"))),
                    ),
                    InlineKeyboardButton(
                        _("♻️ Used only"),
                        callback_data=encode(Action("product.offer_filter", (product_id, "u"))),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        _("📦 Amazon only"),
                        callback_data=encode(Action("product.offer_filter", (product_id, "s1"))),
                    ),
                    InlineKeyboardButton(
                        _("🏪 Any seller"),
                        callback_data=encode(Action("product.offer_filter", (product_id, "s0"))),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        _("👍 Anything is fine (default)"),
                        callback_data=encode(Action("product.offer_filter", (product_id, "0"))),
                    ),
                ],
            ]
        )
    else:
        # Non-Amazon: show threshold menu directly
        lines.append(_("\n<b>How do you want to be notified?</b>"))
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
