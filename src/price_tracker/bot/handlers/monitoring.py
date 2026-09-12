"""Price-monitoring handlers: /check, /checkall, /refresh, /pausa, /riattiva.

Ported from monolithic bot.py [Task 17]. The periodic scrape loop lives in
``core.scheduler.Scheduler``; this module exposes the interactive Telegram
commands and the rich :func:`_send_alert` helper used by both modes when a
threshold fires.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from price_tracker.bot.decorators import (
    _config,
    _convert_display,
    _db,
    restricted,
    with_locale,
)
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _get_user_product,
    _parse_id,
    _safe_dec,
)
from price_tracker.bot.messages import _, ngettext

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _product_picker_args() -> tuple[str, str]:
    """Sentinel — kept for future extraction."""
    return ("", "")


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
        await update.message.reply_text(_("📭 You have no tracked products."))
        return True

    buttons = []
    for p in products:
        name = (p.get("name") or _("Unknown"))[:35]
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


@with_locale
@restricted
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set per-product check interval. Usage: /refresh <id> <minutes>"""
    if not context.args:
        await _product_picker(
            update,
            context,
            "refresh",
            _("Choose product to set interval"),
            "setrefresh",
        )
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            _(
                "❌ Usage: /refresh &lt;id&gt; &lt;minutes&gt;\n\n"
                "Examples:\n"
                "<code>/refresh 3 30</code> — check every 30 minutes\n"
                "<code>/refresh 3 720</code> — check every 12 hours\n"
                "<code>/refresh 3 0</code> — return to global interval"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ Invalid ID."))
        return

    try:
        minutes = int(context.args[1])
    except ValueError:
        await update.message.reply_text(_("❌ Invalid value. Enter a number of minutes."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Product not found."))
        return

    db = _db(context)

    if minutes <= 0:
        await db.set_product_interval(product_id, None)
        config = _config(context)
        name_safe = _escape_html((product.get("name") or _("Unknown"))[:80])
        await update.message.reply_text(
            _("🔄 Interval reset to global (every {min} min)\n📦 #{pid} — {name}").format(
                min=config.check_interval_minutes, pid=product_id, name=name_safe
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if minutes < 5:
        await update.message.reply_text(_("❌ Minimum interval is 5 minutes."))
        return
    if minutes > 1440 * 7:
        await update.message.reply_text(_("❌ Maximum interval is 7 days."))
        return

    await db.set_product_interval(product_id, minutes)
    name = product.get("name") or _("Unknown")

    if minutes >= 60:
        hours = minutes / 60
        hours_str = f"{hours:.0f}" if hours == int(hours) else f"{hours:.1f}"
        n_hours = int(hours) if hours == int(hours) else 2  # use plural form for fractional hours
        display = ngettext("{n} hour", "{n} hours", n_hours).format(n=hours_str)
    else:
        display = _("{n} minutes").format(n=minutes)

    await update.message.reply_text(
        _("🔄 Check interval: <b>every {display}</b>\n📦 #{pid} — {name}").format(
            display=display, pid=product_id, name=_escape_html(name[:80])
        ),
        parse_mode=ParseMode.HTML,
    )


@with_locale
@restricted
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check a single product price on demand."""
    if not context.args:
        await _product_picker(update, context, "check", _("Choose product to check"), "check")
        return

    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ Invalid ID."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Product not found."))
        return

    is_active = product.get("is_active", 0)
    if not is_active:
        await update.message.reply_text(_("❌ Product paused. Use /reactivate to resume tracking."))
        return

    msg = await update.message.reply_text(_("🔍 Checking..."))
    scheduler = context.bot_data["scheduler"]
    result = await scheduler.check_one_product_for_user(
        product_id=product_id, user_id=update.effective_user.id
    )
    alert = result.alert

    product = await _db(context).get_product(product_id) or {}
    name = product.get("name") or _("Unknown")
    current = _safe_dec(product.get("current_price"))
    price_str = f"€{current:.2f}" if current else _("N/A")

    if alert:
        # Manual /check command: delete placeholder and send photo+caption when possible
        with contextlib.suppress(Exception):
            await msg.delete()
        await _send_alert(context.bot, alert, _db(context), chat_id=update.effective_user.id)
    else:
        await msg.edit_text(
            _("✅ <b>{name}</b>\n💰 Price: {price}\n📊 No significant change.").format(
                name=_escape_html(name[:80]), price=price_str
            ),
            parse_mode=ParseMode.HTML,
        )


@with_locale
@restricted
async def cmd_checkall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-check every active product owned by the caller."""
    db = _db(context)
    user_id = update.effective_user.id
    products = await db.get_active_products(user_id)

    if not products:
        await update.message.reply_text(_("📭 No products to check."))
        return

    msg = await update.message.reply_text(_("🔍 Checking {n} products...").format(n=len(products)))
    from price_tracker.core.alert import format_alert  # noqa: PLC0415

    scheduler = context.bot_data["scheduler"]
    # Interactive caller: the user is waiting live — override the polite 5s
    # background pacing with a small per-product pause.
    results = await scheduler.check_user_products_for_user(
        user_id=user_id, delay_between_products=0.5
    )
    alerts = [r.alert for r in results if r.alert is not None]

    # Build summary of all products after check
    updated_products = await db.get_active_products(user_id)
    summary_lines = [
        _("✅ <b>Check complete</b> — {n} products").format(n=len(updated_products)) + chr(10)
    ]
    for p in updated_products:
        name = (p.get("name") or _("Unknown"))[:40]
        current = _safe_dec(p.get("current_price"))
        initial = _safe_dec(p.get("initial_price"))
        price_str = f"€{current:.2f}" if current else _("N/A")
        diff_str = ""
        if initial and current and initial > 0 and initial != current:
            diff = (initial - current) / initial * 100
            if diff > 0:
                diff_str = f" <i>(-{diff:.1f}%)</i>"
            elif diff < 0:
                diff_str = f" <i>(+{abs(diff):.1f}%)</i>"
        errors = p.get("consecutive_errors", 0)
        err_str = " ⚠️" if errors and errors > 0 else ""
        summary_lines.append(f"  #{p['id']} {_escape_html(name)} — {price_str}{diff_str}{err_str}")

    if alerts:
        summary_lines.append(chr(10) + _("🔔 <b>{n} changes found!</b>").format(n=len(alerts)))

    await msg.edit_text(chr(10).join(summary_lines), parse_mode=ParseMode.HTML)

    for alert in alerts:
        try:
            await _send_alert(context.bot, alert, _db(context), chat_id=update.effective_user.id)
        except Exception as e:  # noqa: BLE001 — fall back to plain text alert
            logger.warning("failed to send rich alert: %s", e)
            await update.message.reply_text(
                format_alert(alert),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )


@with_locale
@restricted
async def cmd_reactivate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-enable a paused product."""
    if not context.args:
        # Show paused products
        db = _db(context)
        user_id = update.effective_user.id
        all_products = await db.get_all_products(user_id)
        paused = [p for p in all_products if not p.get("is_active")]
        if not paused:
            await update.message.reply_text(_("✅ You have no paused products."))
            return
        buttons = []
        for p in paused:
            name = (p.get("name") or _("Unknown"))[:35]
            buttons.append(
                [InlineKeyboardButton(f"#{p['id']} {name}", callback_data=f"reactivate_{p['id']}")]
            )
        await update.message.reply_text(
            _("⏸ <b>Paused products — choose to reactivate:</b>"),
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

    await _db(context).reactivate_product(product_id)
    name = product.get("name") or _("Unknown")
    await update.message.reply_text(
        _("✅ Reactivated: <b>{name}</b>").format(name=_escape_html(name[:80])),
        parse_mode=ParseMode.HTML,
    )


@with_locale
@restricted
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause a tracked product."""
    if not context.args:
        await _product_picker(update, context, "pause", _("Choose product to pause"), "pause")
        return
    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ Invalid ID."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Product not found."))
        return

    await _db(context).deactivate_product(product_id)
    name = product.get("name") or _("Unknown")
    await update.message.reply_text(
        _("⏸ Tracking paused: <b>{name}</b>").format(name=_escape_html(name[:80]))
        + "\n"
        + _("Use /reactivate {pid} to resume tracking.").format(pid=product_id),
        parse_mode=ParseMode.HTML,
    )


# ── Periodic-job helper ───────────────────────────────────────────
# The actual periodic check is registered in ``main.scheduled_check_job`` and
# delegates to :meth:`Scheduler.run_check_all`. The ``_send_alert`` helper
# below is still used by the interactive commands /check and /checkall to
# render the rich photo+caption response.


async def _send_alert(bot: Any, alert: Any, db: Any, *, chat_id: int | None = None) -> None:
    """Send an alert with a price-history chart when possible.

    Falls back to plain text if chart generation fails or price data is
    insufficient (deactivation / availability alerts).

    ``chat_id`` overrides the destination when supplied — used by interactive
    handlers that hold the user's chat id locally. When omitted, falls back to
    ``alert.owner_user_id`` (set by legacy push-mode notifiers).
    """
    from price_tracker.bot.handlers.history import _generate_chart  # noqa: PLC0415
    from price_tracker.core.alert import format_alert  # noqa: PLC0415

    text = format_alert(alert)
    target_chat = chat_id if chat_id is not None else getattr(alert, "owner_user_id", None)
    if target_chat is None:
        logger.warning("Cannot dispatch alert: no chat_id and alert has no owner_user_id")
        return

    # Availability / deactivation alerts don't benefit from the chart.
    skip_chart = getattr(alert, "product_deactivated", False) or (
        getattr(alert, "availability_changed", False) and not getattr(alert, "is_available", True)
    )
    png = None
    if not skip_chart:
        try:
            product = await db.get_product(alert.product_id)
            if product is not None:
                # ``_generate_chart`` accepts a dict-compatible record (ProductRecord
                # implements __getitem__/get via _DictCompatMixin since v0.1.4).
                png = await _generate_chart(db, alert.product_id, product)
        except Exception as e:  # noqa: BLE001 — fall back to plain-text alert on any failure
            logger.warning("chart build failed for product %s: %s", alert.product_id, e)

    if png:
        # Telegram caption limit = 1024 chars; alert text fits comfortably.
        await bot.send_photo(
            chat_id=target_chat,
            photo=png,
            caption=text[:1024],
            parse_mode=ParseMode.HTML,
        )
    else:
        await bot.send_message(
            chat_id=target_chat,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def scheduled_cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily cleanup: compact old price history to 1 record/day."""
    db = context.bot_data["db"]
    try:
        deleted = await db.cleanup_old_history(retention_days=30)
        if deleted > 0:
            logger.info("🧹 Cleanup: removed %s old history records", deleted)
    except Exception as e:  # noqa: BLE001 — telemetry only, scheduler must keep running
        logger.error("Cleanup error: %s", e)


# Re-export `_convert_display` so callers in this module can use the same symbol.
__all__ = [
    "_convert_display",
    "_send_alert",
    "cmd_check",
    "cmd_checkall",
    "cmd_pause",
    "cmd_reactivate",
    "cmd_refresh",
    "scheduled_cleanup",
]


def register(app: Application) -> None:
    """Register monitoring command handlers on `app`."""
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("controlla", cmd_check))
    app.add_handler(CommandHandler("checkall", cmd_checkall))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("riattiva", cmd_reactivate))
    app.add_handler(CommandHandler("reactivate", cmd_reactivate))
    app.add_handler(CommandHandler("pausa", cmd_pause))
    app.add_handler(CommandHandler("pause", cmd_pause))
