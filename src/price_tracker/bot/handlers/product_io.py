"""CSV import/export handlers: /esporta, /importa.

Split out of `handlers/product.py` to keep each module under the 500-LOC
budget [Task 17].
"""

from __future__ import annotations

import contextlib
import csv
import io
import logging
from datetime import datetime
from decimal import Decimal

from telegram import InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from price_tracker.bot.decorators import _client, _db, _scraper, restricted, with_locale
from price_tracker.bot.handlers.callbacks._menu import CSV_HEADERS
from price_tracker.bot.messages import _

logger = logging.getLogger(__name__)

# Import accepts both the current English headers and the Italian ones written
# by releases up to 1.0.0, so a CSV exported before the i18n sweep still loads.
CSV_ALIASES: dict[str, tuple[str, ...]] = {
    "Name": ("Name", "Nome"),
    "URL": ("URL",),
    "Target": ("Target",),
    "Threshold": ("Threshold", "Soglia"),
    "Currency": ("Currency", "Valuta"),
}


def _cell(row: dict[str, str], field: str, default: str = "") -> str:
    """Read `field` from a CSV row, falling back to its legacy header names."""
    for key in CSV_ALIASES[field]:
        value = row.get(key)
        if value is not None and value.strip():
            return value
    return default


@with_locale
@restricted
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export user products as CSV file."""
    db = _db(context)
    user_id = update.effective_user.id
    products = await db.get_all_products(user_id)

    if not products:
        await update.message.reply_text(_("📭 You have no products to export."))
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    for p in products:
        writer.writerow(
            [
                p["id"],
                p.get("name", ""),
                p.get("url", ""),
                p.get("initial_price", ""),
                p.get("current_price", ""),
                p.get("lowest_price", ""),
                p.get("target_price", ""),
                f"{p.get('threshold_type', 'percentage')}:{p.get('threshold_value', '10')}",
                "Yes" if p.get("is_active") else "No",
                p.get("currency", "EUR"),
            ]
        )

    csv_bytes = buf.getvalue().encode("utf-8")
    filename = f"products_{datetime.now().strftime('%Y%m%d')}.csv"
    await update.message.reply_document(
        document=InputFile(io.BytesIO(csv_bytes), filename=filename),
        caption=_("📊 {count} products exported.").format(count=len(products)),
    )


@with_locale
@restricted
async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Import products from a CSV file."""
    if not update.message.document:
        await update.message.reply_text(
            _(
                "📁 <b>Import products from CSV</b>\n\n"
                "Send a CSV file (the one produced by /export) as an attachment.\n"
                "Duplicate products (same URL) will be skipped."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    doc = update.message.document
    if not doc.file_name or not doc.file_name.endswith(".csv"):
        await update.message.reply_text(_("❌ The file must be a CSV."))
        return

    file = await context.bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)

    try:
        reader = csv.DictReader(io.StringIO(buf.read().decode("utf-8")))
    except Exception as e:  # noqa: BLE001 — surface parse error to user
        await update.message.reply_text(_("❌ Error parsing the CSV: {error}").format(error=e))
        return

    db = _db(context)
    client = _client(context)
    scraper = _scraper(context)
    user_id = update.effective_user.id
    imported = 0
    skipped = 0
    errors = 0

    msg = await update.message.reply_text(_("⏳ Import in progress..."))

    from price_tracker.core.url_utils import extract_etld_plus_one  # noqa: PLC0415

    for row in reader:
        url = _cell(row, "URL").strip()
        if not url:
            continue

        # Skip duplicates
        existing = await db.get_product_by_url_for_user(url, user_id)
        if existing:
            skipped += 1
            continue

        try:
            domain = extract_etld_plus_one(url)
            scraper_for_url = scraper.resolve(url)
            if scraper_for_url is None:
                errors += 1
                continue
            result = await scraper_for_url.scrape(url, client)
            price = result.price
            name = result.name or _cell(row, "Name", _("Imported"))

            # Use CSV target if available
            target_str = _cell(row, "Target").strip()
            target = None
            if target_str:
                with contextlib.suppress(ValueError, ArithmeticError):
                    target = Decimal(target_str)

            # Parse threshold from CSV
            threshold_str = _cell(row, "Threshold", "percentage:10")
            th_type, th_value = "percentage", "10"
            if ":" in threshold_str:
                parts = threshold_str.split(":", 1)
                th_type, th_value = parts[0], parts[1]

            currency = _cell(row, "Currency", "EUR").strip() or "EUR"

            new_pid = await db.add_product(
                user_id=user_id,
                url=url,
                name=name,
                domain=domain,
                initial_price=price,
                threshold_type=th_type,
                threshold_value=Decimal(th_value),
                currency=currency,
            )
            if target is not None:
                await db.set_target_price(new_pid, target)
            imported += 1
        except Exception as e:  # noqa: BLE001 — log + count and keep going
            logger.error("Import error for %s: %s", url[:60], e)
            errors += 1

    lines = [_("📁 <b>Import complete</b>")]
    lines.append(_("✅ Imported: {count}").format(count=imported))
    if skipped:
        lines.append(_("⏭️ Duplicates skipped: {count}").format(count=skipped))
    if errors:
        lines.append(_("❌ Errors: {count}").format(count=errors))
    await msg.edit_text(chr(10).join(lines), parse_mode=ParseMode.HTML)


def register(app: Application) -> None:
    """Register CSV import/export handlers on `app`."""
    app.add_handler(CommandHandler("esporta", cmd_export))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(MessageHandler(filters.Document.FileExtension("csv"), cmd_import))
    app.add_handler(CommandHandler("importa", cmd_import))
