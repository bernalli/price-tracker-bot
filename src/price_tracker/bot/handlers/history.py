"""Price-history & reset handlers: /history, /reset.

Ported from monolithic bot.py. The chart renderer (`_generate_chart`)
is kept here as a private helper until the chart module gets its own home.
"""

from __future__ import annotations

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


async def _generate_chart(db: Any, product_id: int, product: dict[str, Any]) -> io.BytesIO | None:
    """Generate a price-history chart as PNG image. Returns None if data is too sparse.

    Note: matplotlib imports are deferred to avoid the heavy import at module
    load — the bot starts faster, and headless test imports stay cheap.
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

    import matplotlib  # noqa: PLC0415 — heavy import deferred

    matplotlib.use("Agg")
    import matplotlib.dates as mdates  # noqa: PLC0415
    import matplotlib.pyplot as plt  # noqa: PLC0415

    fig, ax = plt.subplots(figsize=(8, 3.5), dpi=100)
    fig.patch.set_facecolor("#000000")
    ax.set_facecolor("#000000")

    ax.plot(dates, prices, color="#ff9f1c", linewidth=2.2, antialiased=True)

    # Target line
    target = product.get("target_price")
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

    name = (product.get("name") or "Prodotto")[:50]
    ax.set_title(name, color="white", fontsize=10, pad=10)

    min_p, max_p = min(prices), max(prices)
    margin = (max_p - min_p) * 0.15 if max_p != min_p else max_p * 0.05
    ax.set_ylim(min_p - margin, max_p + margin)

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


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
            await update.message.reply_text(_("📭 Non hai prodotti tracciati."))
            return

        buttons = []
        for p in products:
            name = (p.get("name") or "Sconosciuto")[:35]
            buttons.append(
                [InlineKeyboardButton(f"#{p['id']} {name}", callback_data=f"chart_{p['id']}")]
            )

        await update.message.reply_text(
            "📊 <b>Scegli un prodotto per lo storico:</b>",
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

    db = _db(context)
    chart_buf = await _generate_chart(db, product_id, product)
    if chart_buf:
        name = (product.get("name") or "Prodotto")[:50]
        lowest = _safe_dec(product.get("lowest_price"))
        highest = _safe_dec(product.get("highest_price"))
        caption = f"📊 <b>#{product_id}</b> {_escape_html(name)}"
        if lowest:
            caption += f"\n📉 Min: €{lowest:.2f}"
        if highest:
            caption += f"  📈 Max: €{highest:.2f}"
        await update.message.reply_photo(
            photo=InputFile(chart_buf, filename=f"chart_{product_id}.png"),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            _("📭 Dati insufficienti per generare il grafico (servono almeno 2 punti).")
        )


@with_locale
@restricted
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset initial_price to current_price for a product."""
    if not context.args:
        await update.message.reply_text(
            "❌ Uso: /reset &lt;id&gt;\n\n"
            "Reimposta il prezzo iniziale al prezzo corrente.\n"
            "Utile quando il prezzo è sceso e vuoi azzerare il confronto.",
            parse_mode=ParseMode.HTML,
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

    db = _db(context)
    success = await db.reset_initial_price(product_id)
    if success:
        name = (product.get("name") or "Sconosciuto")[:60]
        current = _safe_dec(product.get("current_price"))
        price_str = f"€{current:.2f}" if current else "N/D"
        await update.message.reply_text(
            f"✅ Prezzo iniziale aggiornato!\n\n"
            f"📦 <b>#{product_id}</b> {_escape_html(name)}\n"
            f"💰 Nuovo prezzo base: <b>{price_str}</b>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(_("❌ Impossibile aggiornare il prezzo iniziale."))


def register(app: Application) -> None:
    """Register history/reset command handlers on `app`."""
    app.add_handler(CommandHandler("storia", cmd_history))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("azzera", cmd_reset))
