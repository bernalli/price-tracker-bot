"""`/lista` handler — list the user's tracked products with per-row buttons.

Split out of `handlers/product.py` to keep each module under the 500-LOC
budget [Task 17].
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from price_tracker.bot.decorators import _convert_display, _db, restricted, with_locale
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_relative_time,
    _format_threshold,
    _safe_dec,
)
from price_tracker.bot.messages import _

logger = logging.getLogger(__name__)


@with_locale
@restricted
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the user's tracked products."""
    db = _db(context)
    user_id = update.effective_user.id
    products = await db.get_active_products(user_id)

    if not products:
        await update.message.reply_text(
            _("📭 You have no tracked products.\nPaste me a link to get started!")
        )
        return

    await update.message.reply_text(
        _("<b>📦 Your products ({count})</b>").format(count=len(products)),
        parse_mode=ParseMode.HTML,
    )

    # Deferred import: scrapers package may evolve (Task 11+).
    from price_tracker.core.scraper_base import detect_currency  # noqa: PLC0415

    for p in products:
        pid = p["id"]
        name = p.get("name") or _("Unknown")
        name_short = name[:60] + ("..." if len(name) > 60 else "")
        current = _safe_dec(p.get("current_price"))
        initial = _safe_dec(p.get("initial_price"))
        target = _safe_dec(p.get("target_price"))
        lowest = _safe_dec(p.get("lowest_price"))
        url = p.get("url", "")
        currency = p.get("currency", "") or detect_currency(url) or "EUR"
        price_str = _convert_display(current, currency) if current else _("N/A")
        threshold = _format_threshold(
            p.get("threshold_type", "percentage"),
            p.get("threshold_value", "10"),
        )

        parts = [f"<b>#{pid}</b> {_escape_html(name_short)}", f"💰 {price_str}"]

        if initial and current and initial != current and initial > 0:
            diff = (initial - current) / initial * 100
            if diff > 0:
                parts.append(
                    _(
                        "📌 Initial price: €{initial:.2f} (<i>-{diff:.1f}% since tracking</i>)"
                    ).format(initial=initial, diff=diff)
                )
            elif diff < 0:
                increase = abs(diff)
                parts.append(
                    _(
                        "📈 Initial price: €{initial:.2f} (<i>+{increase:.1f}% since tracking</i>)"
                    ).format(initial=initial, increase=increase)
                )

        if lowest and current and lowest < current:
            parts.append(f"📉 Min: €{lowest:.2f}")

        parts.append(_("🎯 Threshold: {threshold}").format(threshold=threshold))
        if target:
            parts.append(f"🏁 Target: €{target:.2f}")

        custom_int = p.get("check_interval_minutes")
        if custom_int:
            if custom_int >= 60:
                h = custom_int / 60
                int_str = f"{h:.0f}h" if h == int(h) else f"{h:.1f}h"
            else:
                int_str = f"{custom_int}min"
            parts.append(_("🔄 Check: every {interval}").format(interval=int_str))

        # Last check time
        ago = _format_relative_time(p.get("last_checked_at"))
        if ago:
            parts.append(_("🕐 Last check: {ago}").format(ago=ago))

        errors = p.get("consecutive_errors", 0)
        if errors and errors > 0:
            parts.append(
                _("⚠️ {count} failed reads recently — details with /errors").format(count=errors)
            )

        text = "\n".join(parts)

        btn_rows = [
            [
                InlineKeyboardButton(_("🔍 Check"), callback_data=f"check_{pid}"),
                InlineKeyboardButton(_("📊 Price history"), callback_data=f"chart_{pid}"),
            ],
            [
                InlineKeyboardButton(_("⏸ Pause"), callback_data=f"pause_{pid}"),
                InlineKeyboardButton(_("🗑 Delete"), callback_data=f"remove_{pid}"),
            ],
            [
                InlineKeyboardButton(_("✏️ Edit"), callback_data=f"edit_{pid}"),
            ],
        ]
        if url:
            btn_rows[2].insert(0, InlineKeyboardButton(_("🔗 Open"), url=url))
        keyboard = InlineKeyboardMarkup(btn_rows)

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )

    # "Delete all" button at the end
    if len(products) > 1:
        keyboard_all = InlineKeyboardMarkup(
            [[InlineKeyboardButton(_("🗑 Delete all products"), callback_data="delete_all")]]
        )
        await update.message.reply_text(
            _("───────────────\n📦 <b>{count}</b> tracked products").format(count=len(products)),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_all,
        )


def register(app: Application) -> None:
    """Register the /lista command handlers on `app`."""
    app.add_handler(CommandHandler("lista", cmd_list))
    app.add_handler(CommandHandler("list", cmd_list))
