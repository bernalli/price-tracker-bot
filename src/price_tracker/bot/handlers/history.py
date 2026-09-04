"""Price-history & reset handlers: /history, /reset.

Ported from monolithic bot.py [Task 17]. The chart renderer (`_generate_chart`)
is kept here as a private helper until the chart module gets its own home in
Plan 3 (F4).
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from price_tracker.bot.decorators import _db, restricted, with_locale
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _get_user_product,
    _parse_id,
    _safe_dec,
)
from price_tracker.bot.messages import _

logger = logging.getLogger(__name__)


def _render_chart(
    dates: list[datetime], prices: list[float], target: object, name: str
) -> io.BytesIO:
    """Render the price-history chart to a PNG buffer (pure CPU — run via to_thread).

    Uses the matplotlib OO API (Figure, not pyplot) so concurrent renders in the
    threadpool don't race on pyplot's global figure registry, and no plt.close()
    bookkeeping is needed. matplotlib imports stay deferred for fast startup.
    """
    import matplotlib  # noqa: PLC0415 — heavy import deferred

    matplotlib.use("Agg")
    import matplotlib.dates as mdates  # noqa: PLC0415
    from matplotlib.figure import Figure  # noqa: PLC0415

    fig = Figure(figsize=(8, 3.5), dpi=100)
    ax = fig.subplots()
    fig.patch.set_facecolor("#000000")
    ax.set_facecolor("#000000")

    ax.plot(dates, prices, color="#ff9f1c", linewidth=2.2, antialiased=True)

    if target:
        try:
            target_f = float(target)
            ax.axhline(
                y=target_f,
                color="#ff6b6b",
                linestyle="--",
                linewidth=1,
                alpha=0.8,
                label=f"Target €{target_f:.2f}",
            )
            ax.legend(facecolor="#000000", edgecolor="#333", labelcolor="white", fontsize=8)
        except (ValueError, TypeError):
            pass

    ax.set_ylabel("€", color="white", fontsize=10)
    ax.tick_params(colors="#999999", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#333333")
    ax.spines["bottom"].set_color("#333333")
    ax.grid(axis="y", alpha=0.15, color="#555555")

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.autofmt_xdate(rotation=30)

    ax.set_title(name, color="white", fontsize=10, pad=10)

    min_p, max_p = min(prices), max(prices)
    margin = (max_p - min_p) * 0.15 if max_p != min_p else max_p * 0.05
    ax.set_ylim(min_p - margin, max_p + margin)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    return buf


async def _generate_chart(db: Any, product_id: int, product: dict[str, Any]) -> io.BytesIO | None:
    """Generate a price-history chart as PNG. Returns None if data is too sparse.

    The matplotlib render is offloaded to a worker thread so it never blocks the
    event loop (and therefore every other user/handler) while drawing.
    """
    history = await db.get_price_history(product_id, limit=100)
    if not history or len(history) < 2:
        return None

    dates: list[datetime] = []
    prices: list[float] = []
    for record in history:
        try:
            dt = datetime.fromisoformat(record["checked_at"].replace("Z", "+00:00"))
            price = float(record["price"])
            dates.append(dt)
            prices.append(price)
        except (ValueError, TypeError):
            continue

    if len(dates) < 2:
        return None

    name = (product.get("name") or _("Product"))[:50]
    return await asyncio.to_thread(_render_chart, dates, prices, product.get("target_price"), name)


@with_locale
@restricted
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a price-history chart for a product."""
    if not context.args:
        # Show product picker
        db = _db(context)
        user_id = update.effective_user.id
        products = await db.get_active_products(user_id)
        if not products:
            await update.message.reply_text(_("📭 You have no tracked products."))
            return

        buttons = []
        for p in products:
            name = (p.get("name") or _("Unknown"))[:35]
            buttons.append(
                [InlineKeyboardButton(f"#{p['id']} {name}", callback_data=f"chart_{p['id']}")]
            )

        await update.message.reply_text(
            _("📊 <b>Pick a product to see its history:</b>"),
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

    db = _db(context)
    chart_buf = await _generate_chart(db, product_id, product)
    if chart_buf:
        name = (product.get("name") or _("Product"))[:50]
        lowest = _safe_dec(product.get("lowest_price"))
        highest = _safe_dec(product.get("highest_price"))
        caption = f"📊 <b>#{product_id}</b> {_escape_html(name)}"
        if lowest:
            caption += _("\n📉 Min: €{price:.2f}").format(price=lowest)
        if highest:
            caption += _("  📈 Max: €{price:.2f}").format(price=highest)
        await update.message.reply_photo(
            photo=InputFile(chart_buf, filename=f"chart_{product_id}.png"),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            _("📭 Not enough data to generate the chart (at least 2 points needed).")
        )


@with_locale
@restricted
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset initial_price to current_price for a product."""
    if not context.args:
        await update.message.reply_text(
            _(
                "❌ Usage: /reset &lt;id&gt;\n\n"
                "Resets the initial price to the current price.\n"
                "Useful when the price has dropped and you want to rebase the comparison."
            ),
            parse_mode=ParseMode.HTML,
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

    db = _db(context)
    success = await db.reset_initial_price(product_id)
    if success:
        name = (product.get("name") or _("Unknown"))[:60]
        current = _safe_dec(product.get("current_price"))
        price_str = f"€{current:.2f}" if current else _("N/A")
        await update.message.reply_text(
            _(
                "✅ Initial price updated!\n\n"
                "📦 <b>#{pid}</b> {name}\n"
                "💰 New base price: <b>{price}</b>"
            ).format(pid=product_id, name=_escape_html(name), price=price_str),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(_("❌ Could not update the initial price."))


def register(app: Application) -> None:
    """Register history/reset command handlers on `app`."""
    app.add_handler(CommandHandler("storia", cmd_history))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("azzera", cmd_reset))
