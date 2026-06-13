"""URL & text-input intake handlers.

Split out of `handlers/product.py` to keep each module under the 500-LOC
budget [item 17]. Handles paste-link UX (`handle_url`) and pending-action
text replies (`handle_text_input`).
"""

from __future__ import annotations

import contextlib
import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
)

from price_tracker.bot.decorators import _config, _convert_display, _db, restricted, with_locale
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_threshold,
    _get_user_product,
    _parse_threshold_input,
    _safe_dec,
)
from price_tracker.bot.handlers.settings import _reschedule_periodic_check
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


@with_locale
@restricted
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect URLs in plain-text messages and treat them as /add input."""
    # Local import — avoids module-load cycles with `handlers.product`.
    from price_tracker.bot.handlers.product import URL_PATTERN, _add_product  # noqa: PLC0415

    text = update.message.text or ""
    match = URL_PATTERN.search(text)
    if not match:
        return

    url = match.group(0).rstrip(".,;:!?)")
    await _add_product(update, context, url)


@with_locale
@restricted
async def handle_text_input(  # noqa: PLR0915 — verbatim port; cyclomatic split planned for F6
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle non-URL plain-text input that satisfies a pending inline-button action."""
    from price_tracker.bot.handlers.product import URL_PATTERN  # noqa: PLC0415

    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if URL_PATTERN.search(text):
        return

    # Handle pending actions from inline button pickers
    pending_action = context.user_data.get("pending_action")
    if not pending_action:
        return

    action_type, product_id = pending_action
    del context.user_data["pending_action"]

    if text.lower() in ("no", "skip", "salta", "-", "annulla"):
        await update.message.reply_text(_("👍 Ok, nessuna modifica."))
        return

    db = _db(context)
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await update.message.reply_text(_("❌ Prodotto non trovato."))
        return
    name = (product.get("name") or "Sconosciuto")[:60]

    if action_type == "target":
        try:
            target = Decimal(text.replace(",", ".").replace("€", "").strip())
        except (InvalidOperation, ValueError):
            await update.message.reply_text(_("❌ Prezzo non valido. Riprova."))
            context.user_data["pending_action"] = pending_action
            return
        if target <= 0:
            await db.set_target_price(product_id, None)
            await update.message.reply_text(f"🎯 Target rimosso per #{product_id}.")
        else:
            await db.set_target_price(product_id, target)
            current = _safe_dec(product.get("current_price"))
            currency = product.get("currency", "EUR")
            target_display = _convert_display(target, currency)
            msg = f"🎯 Target: <b>{target_display}</b>\n📦 {_escape_html(name)}"
            if current and target < current:
                diff_pct = ((current - target) / current) * 100
                current_display = _convert_display(current, currency)
                msg += f"\n💰 Attuale: {current_display} (-{diff_pct:.1f}% necessario)"
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    elif action_type == "threshold":
        try:
            threshold_type, threshold_value = _parse_threshold_input(text)
        except ValueError:
            await update.message.reply_text(_("❌ Valore non valido. Riprova (es. 20% o 50)."))
            context.user_data["pending_action"] = pending_action
            return
        await db.set_threshold(product_id, threshold_type, threshold_value)
        threshold_str = _format_threshold(threshold_type, threshold_value)
        await update.message.reply_text(
            f"🎯 Soglia: <b>{threshold_str}</b>\n📦 {_escape_html(name)}",
            parse_mode=ParseMode.HTML,
        )

    elif action_type == "admin_adduser":
        try:
            new_uid = int(text.strip())
        except ValueError:
            await update.message.reply_text(_("❌ ID non valido. Deve essere un numero."))
            context.user_data["pending_action"] = pending_action
            return
        existing = await db.get_user(new_uid)
        if existing and existing.get("is_active"):
            await update.message.reply_text(
                f"ℹ️ Utente <code>{new_uid}</code> già autorizzato.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await db.add_user(new_uid, is_admin=False)
            await update.message.reply_text(
                f"✅ Utente <code>{new_uid}</code> aggiunto!",
                parse_mode=ParseMode.HTML,
            )
            with contextlib.suppress(Exception):
                await context.bot.send_message(
                    chat_id=new_uid,
                    text="🎉 Sei stato autorizzato! Invia /start.",
                )

    elif action_type == "admin_nick":
        nickname = text.strip()
        if not nickname:
            await update.message.reply_text(_("❌ Nickname vuoto."))
            return
        await db.update_user_info(product_id, display_name=nickname)
        await update.message.reply_text(
            f"✅ Nickname aggiornato: <b>{_escape_html(nickname)}</b>",
            parse_mode=ParseMode.HTML,
        )

    elif action_type == "admin_debug":
        url_input = text.strip()
        if not url_input.startswith("http"):
            await update.message.reply_text(_("❌ URL non valido."))
            return
        # Trigger the debug command
        from price_tracker.bot.handlers.debug import cmd_debug  # noqa: PLC0415

        context.args = [url_input]
        await cmd_debug(update, context)

    elif action_type == "admin_interval":
        try:
            minutes = int(text.strip())
        except ValueError:
            await update.message.reply_text(_("❌ Numero non valido."))
            context.user_data["pending_action"] = pending_action
            return
        if minutes < 5:
            await update.message.reply_text(_("❌ Minimo 5 minuti."))
            context.user_data["pending_action"] = pending_action
            return
        if minutes > 1440 * 7:
            await update.message.reply_text(_("❌ L'intervallo massimo è 7 giorni."))
            context.user_data["pending_action"] = pending_action
            return
        await db.set_config("check_interval_minutes", str(minutes))
        _reschedule_periodic_check(context, minutes)
        if minutes >= 60:
            h = minutes / 60
            display = f"{h:.0f} ore" if h == int(h) else f"{h:.1f} ore"
        else:
            display = f"{minutes} minuti"
        await update.message.reply_text(
            f"✅ Intervallo aggiornato: <b>ogni {display}</b>",
            parse_mode=ParseMode.HTML,
        )

    elif action_type == "refresh":
        try:
            minutes = int(text.strip())
        except ValueError:
            await update.message.reply_text(_("❌ Numero non valido. Riprova."))
            context.user_data["pending_action"] = pending_action
            return
        if minutes <= 0:
            await db.set_product_interval(product_id, None)
            config = _config(context)
            await update.message.reply_text(
                f"🔄 Intervallo ripristinato a globale "
                f"({config.check_interval_minutes} min)\n"
                f"📦 {_escape_html(name)}",
                parse_mode=ParseMode.HTML,
            )
        elif minutes < 5:
            await update.message.reply_text(_("❌ Minimo 5 minuti."))
            context.user_data["pending_action"] = pending_action
        else:
            await db.set_product_interval(product_id, minutes)
            if minutes >= 60:
                hours = minutes / 60
                display = f"{hours:.0f} ore" if hours == int(hours) else f"{hours:.1f} ore"
            else:
                display = f"{minutes} minuti"
            await update.message.reply_text(
                f"🔄 Check: ogni <b>{display}</b>\n📦 {_escape_html(name)}",
                parse_mode=ParseMode.HTML,
            )


def register(app: Application) -> None:
    """Register URL/text intake handlers on `app`."""
    from price_tracker.bot.handlers.product import URL_PATTERN  # noqa: PLC0415

    # URL auto-detection
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(URL_PATTERN), handle_url)
    )
    # Generic text for pending inputs
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
