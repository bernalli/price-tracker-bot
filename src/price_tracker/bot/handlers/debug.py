"""Debug & status handlers: /debug, /stato.

Ported from monolithic bot.py [item 17]. The verbose scraper-debug command
exercises every detection path used by the registry; URL/text intake handlers
live in `handlers/product.py` (paste-link UX).
"""

from __future__ import annotations

import json as _json
import logging
import re as _re
from typing import TYPE_CHECKING

import httpx
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from price_tracker.bot.decorators import _client, _config, _db, _scraper, admin_only, restricted
from price_tracker.bot.handlers._helpers import _escape_html
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


@admin_only
async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Debug scraping for a URL — shows what each strategy finds."""
    if not context.args:
        await update.message.reply_text("❌ Uso: /debug <url>", parse_mode=ParseMode.HTML)
        return

    url = context.args[0]
    msg = await update.message.reply_text(_("🔍 Analisi in corso..."))

    from bs4 import BeautifulSoup  # noqa: PLC0415 — heavy import deferred

    from price_tracker.core.scraper_base import get_headers  # noqa: PLC0415

    client = _client(context)
    lines = [f"🔍 <b>Debug scraping</b>\n🔗 {_escape_html(url[:80])}\n"]

    # Step 1: Fetch with httpx
    html = None
    try:
        resp = await client.get(url, headers=get_headers(), follow_redirects=True)
        lines.append(f"📡 httpx shared: <b>HTTP {resp.status_code}</b> ({len(resp.text)} chars)")
        if resp.status_code == 200:
            html = resp.text
            # If suspiciously small, try fresh client
            if len(html) < 80000 and "application/ld+json" not in html:
                lines.append("⚠️ Risposta piccola senza dati strutturati, provo client fresco...")
                try:
                    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as fresh:
                        r2 = await fresh.get(
                            url,
                            headers={
                                "User-Agent": (
                                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                    "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
                                ),
                                "Accept": (
                                    "text/html,application/xhtml+xml,"
                                    "application/xml;q=0.9,*/*;q=0.8"
                                ),
                                "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
                            },
                        )
                        lines.append(
                            f"📡 httpx fresh: <b>HTTP {r2.status_code}</b> ({len(r2.text)} chars)"
                        )
                        if r2.status_code == 200 and len(r2.text) > len(html):
                            html = r2.text
                            lines.append("✅ Client fresco ha ottenuto più dati!")
                except Exception as e:  # noqa: BLE001 — debug surface, never crash
                    lines.append(f"❌ httpx fresh: {str(e)[:60]}")
        elif resp.status_code == 403:
            lines.append("⚠️ 403 — provo curl_cffi...")
            try:
                from curl_cffi.requests import AsyncSession  # noqa: PLC0415

                async with AsyncSession(impersonate="chrome") as session:
                    r2 = await session.get(url, allow_redirects=True, timeout=30)
                    lines.append(
                        f"📡 curl_cffi: <b>HTTP {r2.status_code}</b> ({len(r2.text)} chars)"
                    )
                    if r2.status_code == 200:
                        html = r2.text
            except Exception as e:  # noqa: BLE001 — debug surface, never crash
                lines.append(f"❌ curl_cffi: {str(e)[:60]}")

            if not html:
                lines.append("⚠️ Provo Scrapling...")
                try:
                    from scrapling import Fetcher  # noqa: PLC0415

                    page = Fetcher.get(
                        url, stealthy_headers=True, follow_redirects=True, timeout=30
                    )
                    lines.append(
                        f"📡 Scrapling: <b>HTTP {page.status}</b> "
                        f"({len(page.text) if page.text else 0} chars)"
                    )
                    if page.status == 200 and page.text:
                        html = page.text
                except Exception as e:  # noqa: BLE001 — debug surface, never crash
                    lines.append(f"❌ Scrapling: {str(e)[:60]}")
    except Exception as e:  # noqa: BLE001 — debug surface, never crash
        lines.append(f"❌ httpx: {str(e)[:80]}")

    if not html:
        lines.append("\n❌ Impossibile caricare la pagina con nessun metodo.")
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    soup = BeautifulSoup(html, "lxml")

    # Step 2: Check JSON-LD
    scripts = soup.find_all("script", type="application/ld+json")
    # Also try regex fallback on raw HTML (BS4 sometimes misses scripts)
    if not scripts:
        raw_html = str(soup)
        for m in _re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            raw_html,
            _re.DOTALL,
        ):
            try:
                _json.loads(m.group(1).strip())
                lines.append("📦 JSON-LD: ❌ BS4 non trova gli script, ma regex sì!")
                break
            except _json.JSONDecodeError:
                pass

    if scripts:
        for i, s in enumerate(scripts[:3]):
            try:
                raw = s.string or s.get_text(strip=True)
                if not raw:
                    lines.append(f"📦 JSON-LD #{i + 1}: contenuto vuoto")
                    continue
                data = _json.loads(raw)
                tp = data.get("@type", "?")
                offers = data.get("offers", data.get("Offers"))
                has_offers = offers is not None
                lines.append(
                    f"📦 JSON-LD #{i + 1}: type=<b>{tp}</b> offers={'✅' if has_offers else '❌'}"
                )
                if has_offers:
                    if isinstance(offers, dict):
                        p = offers.get("price", "?")
                        curr = offers.get("priceCurrency", "?")
                        lines.append(f"   → price: {p} {curr}")
                    elif isinstance(offers, list):
                        for o in offers[:2]:
                            lines.append(f"   → price: {o.get('price', '?')}")
            except Exception as e:  # noqa: BLE001 — debug parse surface
                lines.append(f"📦 JSON-LD #{i + 1}: parse error: {str(e)[:40]}")
    else:
        lines.append("📦 JSON-LD: ❌ non trovato")

    # Step 3: Check OG/meta tags
    og_price = soup.find("meta", property="og:price:amount") or soup.find(
        "meta", attrs={"name": "og:price:amount"}
    )
    if og_price:
        lines.append(f"🏷 og:price:amount: <b>{og_price.get('content', '?')}</b>")
    product_price = soup.find("meta", property="product:price:amount")
    if product_price:
        lines.append(f"🏷 product:price:amount: <b>{product_price.get('content', '?')}</b>")
    if not og_price and not product_price:
        lines.append("🏷 OG/meta price: ❌ non trovato")

    # Step 4: Check microdata
    itemprop_price = soup.find(attrs={"itemprop": "price"})
    if itemprop_price:
        val = itemprop_price.get("content") or itemprop_price.get_text(strip=True)
        lines.append(f"🔖 itemprop=price: <b>{str(val)[:30]}</b>")
    else:
        lines.append("🔖 itemprop=price: ❌ non trovato")

    # Step 5: Check common selectors
    found_css = False
    for sel in [
        ".product-price",
        ".price",
        "[data-price]",
        ".woocommerce-Price-amount",
        ".current-price",
        ".sale-price",
        ".price--selling",
    ]:
        el = soup.select_one(sel)
        if el:
            val = el.get("data-price") or el.get("content") or el.get_text(strip=True)
            val_str = str(val) if val else ""
            lines.append(f"🎯 CSS '{sel}': <b>{_escape_html(val_str[:40])}</b>")
            found_css = True
    if not found_css:
        lines.append("🎯 CSS selectors: ❌ nessun match")

    # Step 6: Regex price in first 3000 chars of body
    body = soup.find("body")
    if body:
        text = body.get_text(separator=" ")[:3000]
        price_matches = _re.findall(r"€\s*\d+[.,]\d{2}|\d+[.,]\d{2}\s*€", text)
        if price_matches:
            lines.append(f"🔎 Regex €: {', '.join(price_matches[:5])}")
        else:
            lines.append("🔎 Regex €: ❌ nessun match")

    # Step 7: Title
    title = soup.find("title")
    if title and title.string:
        lines.append(f"\n📝 Title: {_escape_html(title.string.strip()[:80])}")

    # Step 8: Run actual scraper
    scraper = _scraper(context)
    result = await scraper.scrape(url, client)
    lines.append("\n🤖 <b>Risultato scraper:</b>")
    lines.append(f"   Nome: {_escape_html((result.name or '❌')[:60])}")
    price_repr = "€" + str(result.price) if result.price else "❌ " + (result.error or "")
    lines.append(f"   Prezzo: {price_repr}")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user / admin statistics for the active bot."""
    db = _db(context)
    config = _config(context)
    user_id = update.effective_user.id
    is_admin = await db.is_user_admin(user_id)

    user_stats = await db.get_stats(user_id)
    saved_interval = await db.get_config("check_interval_minutes")
    interval = int(saved_interval) if saved_interval else config.check_interval_minutes

    if interval >= 60:
        hours = interval / 60
        interval_str = f"{hours:.0f}h" if hours == int(hours) else f"{hours:.1f}h"
    else:
        interval_str = f"{interval}min"

    lines = [
        "📊 <b>Le tue statistiche</b>\n",
        f"📦 Prodotti attivi: {user_stats['active_products']}",
        f"📁 Prodotti totali: {user_stats['total_products']}",
        f"🔍 Controlli effettuati: {user_stats['total_checks']}",
        f"⏱ Intervallo check: ogni {interval_str}",
    ]

    if is_admin:
        global_stats = await db.get_stats()
        users = await db.get_all_users()
        lines.extend(
            [
                "",
                "<b>👑 Panoramica admin</b>",
                f"👥 Utenti attivi: {len(users)}",
                f"📦 Prodotti totali (globali): {global_stats['active_products']}",
                f"🔍 Check totali (globali): {global_stats['total_checks']}",
            ]
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def register(app: Application) -> None:
    """Register debug/status command handlers on `app`."""
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("stato", cmd_status))
    app.add_handler(CommandHandler("status", cmd_status))
