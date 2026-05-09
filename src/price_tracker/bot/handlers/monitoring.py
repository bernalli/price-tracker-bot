"""Price-monitoring handlers: /check, /checkall, /refresh, /pausa, /riattiva.

Ported from monolithic bot.py. Scheduled jobs (`scheduled_check`,
`scheduled_cleanup`) and the rich alert sender (`_send_alert`) live here for
now — they move into `core/scheduler.py` once the standalone
`PriceChecker` is back online.
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
    _client,
    _config,
    _convert_display,
    _db,
    _scraper,
    restricted,
)
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _get_user_product,
    _parse_id,
    _safe_dec,
)
from price_tracker.bot.messages import _

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
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set per-product check interval. Usage: /refresh <id> <minuti>"""
    if not context.args:
        await _product_picker(
            update,
            context,
            "refresh",
            "Scegli prodotto per impostare intervallo",
            "setrefresh",
        )
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Uso: /refresh &lt;id&gt; &lt;minuti&gt;\n\n"
            "Esempi:\n"
            "<code>/refresh 3 30</code> — controlla ogni 30 minuti\n"
            "<code>/refresh 3 720</code> — controlla ogni 12 ore\n"
            "<code>/refresh 3 0</code> — torna all'intervallo globale",
            parse_mode=ParseMode.HTML,
        )
        return

    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ ID non valido."))
        return

    try:
        minutes = int(context.args[1])
    except ValueError:
        await update.message.reply_text(_("❌ Valore non valido. Inserisci un numero di minuti."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Prodotto non trovato."))
        return

    db = _db(context)

    if minutes <= 0:
        await db.set_product_interval(product_id, None)
        config = _config(context)
        await update.message.reply_text(
            f"🔄 Intervallo ripristinato a quello globale "
            f"(ogni {config.check_interval_minutes} min)\n"
            f"📦 #{product_id} — "
            f"{_escape_html((product.get('name') or 'Sconosciuto')[:80])}",
            parse_mode=ParseMode.HTML,
        )
        return

    if minutes < 5:
        await update.message.reply_text(_("❌ L'intervallo minimo è 5 minuti."))
        return
    if minutes > 1440 * 7:
        await update.message.reply_text(_("❌ L'intervallo massimo è 7 giorni."))
        return

    await db.set_product_interval(product_id, minutes)
    name = product.get("name") or "Sconosciuto"

    if minutes >= 60:
        hours = minutes / 60
        display = f"{hours:.0f} ore" if hours == int(hours) else f"{hours:.1f} ore"
    else:
        display = f"{minutes} minuti"

    await update.message.reply_text(
        f"🔄 Intervallo check: <b>ogni {display}</b>\n"
        f"📦 #{product_id} — {_escape_html(name[:80])}",
        parse_mode=ParseMode.HTML,
    )


@restricted
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check a single product price on demand."""
    if not context.args:
        await _product_picker(update, context, "check", "Scegli prodotto da controllare", "check")
        return

    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ ID non valido."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Prodotto non trovato."))
        return

    is_active = product.get("is_active", 0)
    if not is_active:
        await update.message.reply_text(_("❌ Prodotto non attivo. Usa /riattiva per riattivarlo."))
        return

    msg = await update.message.reply_text(_("🔍 Controllo in corso..."))
    # Deferred import: PriceChecker lives in legacy `checker.py` for now.
    from checker import PriceChecker  # noqa: PLC0415

    checker = PriceChecker(_config(context), _db(context), _scraper(context))
    alert = await checker.check_product(product, _client(context))

    product = await _db(context).get_product(product_id) or {}
    name = product.get("name") or "Sconosciuto"
    current = _safe_dec(product.get("current_price"))
    price_str = f"€{current:.2f}" if current else "N/D"

    if alert:
        # Manual /check command: delete placeholder and send photo+caption when possible
        with contextlib.suppress(Exception):
            await msg.delete()
        await _send_alert(context.bot, alert, _db(context))
    else:
        await msg.edit_text(
            f"✅ <b>{_escape_html(name[:80])}</b>\n"
            f"💰 Prezzo: {price_str}\n"
            f"📊 Nessuna variazione significativa.",
            parse_mode=ParseMode.HTML,
        )


@restricted
async def cmd_checkall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-check every active product owned by the caller."""
    db = _db(context)
    user_id = update.effective_user.id
    products = await db.get_active_products(user_id)

    if not products:
        await update.message.reply_text(_("📭 Nessun prodotto da controllare."))
        return

    msg = await update.message.reply_text(f"🔍 Controllo di {len(products)} prodotti in corso...")
    # Deferred import: PriceChecker lives in legacy `checker.py` for now.
    from checker import (  # noqa: PLC0415,E501
        PriceChecker,
        format_alert,
    )

    checker = PriceChecker(_config(context), db, _scraper(context))
    alerts = await checker.check_products(products, _client(context))

    # Build summary of all products after check
    updated_products = await db.get_active_products(user_id)
    summary_lines = [f"✅ <b>Controllo completato</b> — {len(updated_products)} prodotti" + chr(10)]
    for p in updated_products:
        name = (p.get("name") or "Sconosciuto")[:40]
        current = _safe_dec(p.get("current_price"))
        initial = _safe_dec(p.get("initial_price"))
        price_str = f"€{current:.2f}" if current else "N/D"
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
        summary_lines.append(chr(10) + f"🔔 <b>{len(alerts)} variazioni trovate!</b>")

    await msg.edit_text(chr(10).join(summary_lines), parse_mode=ParseMode.HTML)

    for alert in alerts:
        try:
            await _send_alert(context.bot, alert, _db(context))
        except Exception as e:  # noqa: BLE001 — fall back to plain text alert
            logger.warning("failed to send rich alert: %s", e)
            await update.message.reply_text(
                format_alert(alert),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )


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
            await update.message.reply_text(_("✅ Non hai prodotti in pausa."))
            return
        buttons = []
        for p in paused:
            name = (p.get("name") or "Sconosciuto")[:35]
            buttons.append(
                [InlineKeyboardButton(f"#{p['id']} {name}", callback_data=f"reactivate_{p['id']}")]
            )
        await update.message.reply_text(
            "⏸ <b>Prodotti in pausa — scegli da riattivare:</b>",
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

    await _db(context).reactivate_product(product_id)
    name = product.get("name") or "Sconosciuto"
    await update.message.reply_text(
        f"✅ Riattivato: <b>{_escape_html(name[:80])}</b>",
        parse_mode=ParseMode.HTML,
    )


@restricted
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause a tracked product."""
    if not context.args:
        await _product_picker(
            update, context, "pause", "Scegli prodotto da mettere in pausa", "pause"
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

    await _db(context).deactivate_product(product_id)
    name = product.get("name") or "Sconosciuto"
    await update.message.reply_text(
        f"⏸ Tracking in pausa: <b>{_escape_html(name[:80])}</b>\n"
        f"Usa /riattiva {product_id} per riprendere.",
        parse_mode=ParseMode.HTML,
    )


# ── Scheduled jobs (to move to core/scheduler.py) ────────────────


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called periodically by the job queue. Checks all products, notifies each owner."""
    # Deferred imports to keep monitoring.py importable while
    # `checker.PriceChecker` and `_fetch_ecb_rates` live in legacy modules.
    from checker import PriceChecker  # noqa: PLC0415

    config = context.bot_data["config"]
    db = context.bot_data["db"]
    scraper = context.bot_data["scraper"]
    client = context.bot_data["http_client"]

    checker = PriceChecker(config, db, scraper)
    # Check ALL active products (across all users)
    alerts = await checker.check_all(client)

    if not alerts:
        return

    for alert in alerts:
        try:
            await _send_alert(context.bot, alert, db)
        except Exception as e:  # noqa: BLE001 — telemetry only, never abort the job
            logger.error("Failed to send alert to %s: %s", alert.owner_user_id, e)


async def _send_alert(bot: Any, alert: Any, db: Any) -> None:
    """Send an alert with a price-history chart when possible.

    Falls back to plain text if chart generation fails or price data is
    insufficient (deactivation / availability alerts).
    """
    from checker import format_alert  # noqa: PLC0415

    # Deferred import: chart module lives in legacy `chart.py` for now.
    text = format_alert(alert)
    # Availability / deactivation alerts don't benefit from the chart.
    skip_chart = getattr(alert, "product_deactivated", False) or (
        getattr(alert, "availability_changed", False) and not getattr(alert, "is_available", True)
    )
    png = None
    if not skip_chart:
        try:
            from chart import (
                render_price_history,  # noqa: PLC0415,E501
            )

            hist = await db.get_price_history(alert.product_id, limit=500)
            png = render_price_history(
                hist,
                alert.product_name,
                alert.new_price,
                initial_price=alert.initial_price,
                target_price=alert.target_price,
            )
        except Exception as e:  # noqa: BLE001 — fall back to plain-text alert on any failure
            logger.warning("chart build failed for product %s: %s", alert.product_id, e)

    if png:
        # Telegram caption limit = 1024 chars; alert text fits comfortably.
        await bot.send_photo(
            chat_id=alert.owner_user_id,
            photo=png,
            caption=text[:1024],
            parse_mode=ParseMode.HTML,
        )
    else:
        await bot.send_message(
            chat_id=alert.owner_user_id,
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
    "scheduled_check",
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
