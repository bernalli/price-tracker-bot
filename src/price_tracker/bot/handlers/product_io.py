"""CSV import/export handlers: /esporta, /importa.

Split out of `handlers/product.py` to keep each module under a 500-line
budget.
"""

from __future__ import annotations

import asyncio
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

from price_tracker.app.inputs import Absolute, Percentage, parse_threshold
from price_tracker.bot.decorators import _client, _db, _scraper, restricted, with_locale
from price_tracker.bot.messages import _

logger = logging.getLogger(__name__)

DEFAULT_CSV_THRESHOLD = ("percentage", Decimal(10))


def _csv_dialect(text: str) -> type[csv.Dialect] | csv.Dialect:
    """Detect a ',' or ';' delimiter from the start of the file; ',' when unsure."""
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;")
    except csv.Error:
        return csv.excel


def parse_csv_threshold(cell: str) -> tuple[str, Decimal] | None:
    """Parse a "Soglia" cell (``<type>:<value>``) with the threshold input grammar.

    An empty cell gives the default ``percentage:10``. ``percentage`` takes an
    integer in 1..99, ``absolute`` an amount > 0 and ``any_drop`` any value, as
    the threshold prompt accepts. Anything else returns None.
    """
    text = cell.strip()
    if not text:
        return DEFAULT_CSV_THRESHOLD
    th_type, separator, value = text.partition(":")
    if not separator:
        return None
    if th_type == "percentage":
        parsed = parse_threshold(f"{value}%")
        return ("percentage", Decimal(parsed.value)) if isinstance(parsed, Percentage) else None
    if th_type == "absolute":
        parsed = parse_threshold(value)
        return ("absolute", parsed.amount) if isinstance(parsed, Absolute) else None
    if th_type == "any_drop":
        return ("any_drop", Decimal(0))
    return None


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
    writer.writerow(
        [
            "ID",
            "Nome",
            "URL",
            "Prezzo Iniziale",
            "Prezzo Attuale",
            "Prezzo Min",
            "Target",
            "Soglia",
            "Attivo",
            "Valuta",
        ]
    )
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
                "Si" if p.get("is_active") else "No",
                p.get("currency", "EUR"),
            ]
        )

    csv_bytes = buf.getvalue().encode("utf-8")
    filename = _("products_{date}.csv").format(date=datetime.now().strftime("%Y%m%d"))
    await update.message.reply_document(
        document=InputFile(io.BytesIO(csv_bytes), filename=filename),
        caption=_("💾 {count} products exported.").format(count=len(products)),
    )


@with_locale
@restricted
async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Import products from a CSV file."""
    if not update.message.document:
        await update.message.reply_text(
            _(
                "📥 <b>Import products from CSV</b>\n\n"
                "Send a CSV file (exported with /export) as an attachment.\n"
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
        text = buf.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(text), dialect=_csv_dialect(text))
        fieldnames = reader.fieldnames or []
    except Exception as e:  # noqa: BLE001 — surface parse error to user
        await update.message.reply_text(_("❌ Error parsing the CSV: {error}").format(error=e))
        return
    if "URL" not in fieldnames:
        await update.message.reply_text(
            _(
                "❌ The CSV file has no URL column. Send the file exported with "
                "/esporta, separated by commas or semicolons."
            )
        )
        return

    db = _db(context)
    client = _client(context)
    scraper = _scraper(context)
    user_id = update.effective_user.id
    imported = 0
    skipped = 0
    errors = 0
    invalid_thresholds = 0

    msg = await update.message.reply_text(_("⏳ Import in progress..."))

    from price_tracker.core.url_utils import (  # noqa: PLC0415
        UnsafeURLError,
        extract_etld_plus_one,
        validate_public_url,
    )

    for row in reader:
        url = row.get("URL", "").strip()
        if not url:
            continue

        # Same SSRF boundary as an interactive addition: a CSV row must not be
        # able to point the bot at loopback, link-local or private addresses.
        # Runs in a thread because getaddrinfo blocks.
        try:
            await asyncio.to_thread(validate_public_url, url)
        except UnsafeURLError as e:
            logger.warning("Rejected unsafe CSV product URL from user %d: %s", user_id, e)
            errors += 1
            continue

        threshold = parse_csv_threshold(row.get("Soglia") or "")
        if threshold is None:
            invalid_thresholds += 1
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
            name = result.name or row.get("Nome", _("Imported"))

            # Use CSV target if available
            target_str = row.get("Target", "").strip()
            target = None
            if target_str:
                with contextlib.suppress(ValueError, ArithmeticError):
                    target = Decimal(target_str)

            currency = row.get("Valuta", "EUR").strip() or "EUR"

            new_pid = await db.add_product(
                user_id=user_id,
                url=url,
                name=name,
                domain=domain,
                initial_price=price,
                threshold_type=threshold[0],
                threshold_value=threshold[1],
                currency=currency,
            )
            if target is not None:
                await db.set_target_price(new_pid, target)
            imported += 1
        except Exception as e:  # noqa: BLE001 — log + count and keep going
            logger.error("Import error for %s: %s", url[:60], e)
            errors += 1

    lines = [_("📥 <b>Import complete</b>")]
    lines.append(_("✅ Imported: {count}").format(count=imported))
    if skipped:
        lines.append(_("⏭️ Duplicates skipped: {count}").format(count=skipped))
    if errors:
        lines.append(_("❌ Errors: {count}").format(count=errors))
    if invalid_thresholds:
        lines.append(_("❌ Rows with an invalid threshold: {n}").format(n=invalid_thresholds))
    await msg.edit_text(chr(10).join(lines), parse_mode=ParseMode.HTML)


def register(app: Application) -> None:
    """Register CSV import/export handlers on `app`."""
    app.add_handler(CommandHandler("esporta", cmd_export))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.Document.FileExtension("csv"), cmd_import
        )
    )
    app.add_handler(CommandHandler("importa", cmd_import))
    app.add_handler(CommandHandler("import", cmd_import))
