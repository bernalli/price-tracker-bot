"""Price-history & reset handlers: /history, /reset.

Ported from monolithic bot.py [Task 17]. The chart renderer (`_generate_chart`)
is kept here as a private helper until the chart module gets its own home in
Plan 3 (F4).
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from datetime import UTC, datetime, timedelta
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

# The chart covers a period, not a number of readings (see _generate_chart).
CHART_WINDOW_DAYS = 90


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

    # Steps, not a diagonal: a price holds until the next change.
    ax.plot(dates, prices, color="#ff9f1c", linewidth=2.2, antialiased=True, drawstyle="steps-post")

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

    The window is a PERIOD, not a row count. Asking for the last 100 readings gave
    a window of `100 x check interval` — about four days on production — and in
    four days almost nothing moves, so every product drew a flat line while its
    real history held several distinct prices.

    The matplotlib render is offloaded to a worker thread so it never blocks the
    event loop (and therefore every other user/handler) while drawing.
    """
    since = (datetime.now(UTC) - timedelta(days=CHART_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    change_points = vars(db).get("get_price_change_points") or getattr(
        type(db), "get_price_change_points", None
    )
    if change_points is None:
        # Third-party repository implementations can adopt the chart query without
        # turning a handler import into an immediate compatibility break.
        history = await db.get_price_history(product_id, limit=100)
    else:
        history = await change_points(product_id, since=since)
    if not history or len(history) < 2:
        return None

    points: list[tuple[datetime, float]] = []
    for record in history:
        try:
            dt = datetime.fromisoformat(record["checked_at"].replace("Z", "+00:00"))
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
            price = float(record["price"])
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
            continue
        # A corrupt value must not pull the axis away from the real price range.
        if not math.isfinite(price) or price <= 0:
            continue
        points.append((dt, price))

    if len(points) < 2:
        return None

    # The repository orders normalized timestamps and uses id as the tie-breaker.
    dates, prices = zip(*points, strict=True)

    name = (product.get("name") or "Prodotto")[:50]
    return await asyncio.to_thread(
        _render_chart, list(dates), list(prices), product.get("target_price"), name
    )


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
