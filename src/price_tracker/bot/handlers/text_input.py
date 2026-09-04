"""URL & text-input intake handlers.

Split out of `handlers/product.py` to keep each module under the 500-LOC
budget [Task 17]. Handles paste-link UX (`handle_url`) and pending-action
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
    _format_minutes,
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

    if text.lower() in ("no", "skip", "salta", "-", "annulla", "cancel"):
        await update.message.reply_text(_("👍 OK, nothing changed."))
        return

    db = _db(context)
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await update.message.reply_text(_("❌ Product not found."))
        return
    name = (product.get("name") or _("Unknown"))[:60]

    if action_type == "target":
        try:
            target = Decimal(text.replace(",", ".").replace("€", "").strip())
        except (InvalidOperation, ValueError):
            await update.message.reply_text(_("❌ Invalid price. Try again."))
            context.user_data["pending_action"] = pending_action
            return
        if target <= 0:
            await db.set_target_price(product_id, None)
            await update.message.reply_text(
                _("🎯 Target cleared for #{pid}.").format(pid=product_id)
            )
        else:
            await db.set_target_price(product_id, target)
            current = _safe_dec(product.get("current_price"))
            currency = product.get("currency", "EUR")
            target_display = _convert_display(target, currency)
            msg = _("🎯 Target: <b>{target}</b>\n📦 {name}").format(
                target=target_display, name=_escape_html(name)
            )
            if current and target < current:
                diff_pct = ((current - target) / current) * 100
                current_display = _convert_display(current, currency)
                msg += _("\n💰 Current: {price} (-{pct:.1f}% needed)").format(
                    price=current_display, pct=diff_pct
                )
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    elif action_type == "threshold":
        try:
            threshold_type, threshold_value = _parse_threshold_input(text)
        except ValueError:
            await update.message.reply_text(_("❌ Invalid value. Try again (e.g. 20% or 50)."))
            context.user_data["pending_action"] = pending_action
            return
        await db.set_threshold(product_id, threshold_type, threshold_value)
        threshold_str = _format_threshold(threshold_type, threshold_value)
        await update.message.reply_text(
            _("🎯 Threshold: <b>{threshold}</b>\n📦 {name}").format(
                threshold=threshold_str, name=_escape_html(name)
            ),
            parse_mode=ParseMode.HTML,
        )

    elif action_type == "admin_adduser":
        try:
            new_uid = int(text.strip())
        except ValueError:
            await update.message.reply_text(_("❌ Invalid ID. It must be a number."))
            context.user_data["pending_action"] = pending_action
            return
        existing = await db.get_user(new_uid)
        if existing and existing.get("is_active"):
            await update.message.reply_text(
                _("ℹ️ User <code>{uid}</code> is already authorized.").format(uid=new_uid),
                parse_mode=ParseMode.HTML,
            )
        else:
            await db.add_user(new_uid, is_admin=False)
            await update.message.reply_text(
                _("✅ User <code>{uid}</code> added!").format(uid=new_uid),
                parse_mode=ParseMode.HTML,
            )
            with contextlib.suppress(Exception):
                await context.bot.send_message(
                    chat_id=new_uid,
                    text=_("🎉 You have been authorized! Send /start."),
                )

    elif action_type == "admin_nick":
        nickname = text.strip()
        if not nickname:
            await update.message.reply_text(_("❌ Empty nickname."))
            return
        await db.update_user_info(product_id, display_name=nickname)
        await update.message.reply_text(
            _("✅ Nickname updated: <b>{name}</b>").format(name=_escape_html(nickname)),
            parse_mode=ParseMode.HTML,
        )

    elif action_type == "admin_debug":
        url_input = text.strip()
        if not url_input.startswith("http"):
            await update.message.reply_text(_("❌ Invalid URL."))
            return
        # Trigger the debug command
        from price_tracker.bot.handlers.debug import cmd_debug  # noqa: PLC0415

        context.args = [url_input]
        await cmd_debug(update, context)

    elif action_type == "admin_interval":
        try:
            minutes = int(text.strip())
        except ValueError:
            await update.message.reply_text(_("❌ Invalid number."))
            context.user_data["pending_action"] = pending_action
            return
        if minutes < 5:
            await update.message.reply_text(_("❌ Minimum is 5 minutes."))
            context.user_data["pending_action"] = pending_action
            return
        if minutes > 1440 * 7:
            await update.message.reply_text(_("❌ The maximum interval is 7 days."))
            context.user_data["pending_action"] = pending_action
            return
        await db.set_config("check_interval_minutes", str(minutes))
        _reschedule_periodic_check(context, minutes)
        await update.message.reply_text(
            _("✅ Interval updated: <b>every {interval}</b>").format(
                interval=_format_minutes(minutes)
            ),
            parse_mode=ParseMode.HTML,
        )

    elif action_type == "refresh":
        try:
            minutes = int(text.strip())
        except ValueError:
            await update.message.reply_text(_("❌ Invalid number. Try again."))
            context.user_data["pending_action"] = pending_action
            return
        if minutes <= 0:
            await db.set_product_interval(product_id, None)
            config = _config(context)
            await update.message.reply_text(
                _("🔄 Interval reset to the global one ({minutes} min)\n📦 {name}").format(
                    minutes=config.check_interval_minutes, name=_escape_html(name)
                ),
                parse_mode=ParseMode.HTML,
            )
        elif minutes < 5:
            await update.message.reply_text(_("❌ Minimum is 5 minutes."))
            context.user_data["pending_action"] = pending_action
        else:
            await db.set_product_interval(product_id, minutes)
            await update.message.reply_text(
                _("🔄 Check: every <b>{interval}</b>\n📦 {name}").format(
                    interval=_format_minutes(minutes), name=_escape_html(name)
                ),
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
