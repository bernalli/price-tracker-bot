"""Debug & status handlers: /debug, /stato.

Ported from monolithic bot.py [Task 17]. The verbose scraper-debug command
exercises every detection path used by the registry; URL/text intake handlers
live in `handlers/product.py` (paste-link UX).
"""

from __future__ import annotations

import json as _json
import logging
import re as _re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from price_tracker.bot.decorators import (
    _client,
    _config,
    _db,
    _scraper,
    admin_only,
    restricted,
    with_locale,
)
from price_tracker.bot.handlers._helpers import _escape_html, _format_relative_time
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from price_tracker.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)


def _format_remaining(until: datetime | None) -> str:
    """Return a human-readable time-until string for a tz-aware expiry datetime."""
    if until is None:
        return "—"
    delta = until - datetime.now(UTC)
    if delta.total_seconds() <= 0:
        return "expired"
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _format_uptime(seconds: float) -> str:
    """Render seconds as a compact `Xh Ym Zs` / `Ym Zs` string."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def _render_metrics_lines(
    metrics: MetricsRegistry | None,
    *,
    start_time: float | None = None,
    products_tracked: int | None = None,
) -> list[str]:
    """Return the metrics-snapshot lines for /status.

    Reads `bot_uptime_seconds` and `products_tracked_total` gauges. When
    `start_time` is provided, refreshes the uptime gauge to `monotonic - start`
    before reading. When `products_tracked` is provided, refreshes that gauge
    too. Uses the prometheus_client private `_value.get()` accessor to read the
    current Gauge value (no public read API exists on Gauge).
    """
    if metrics is None:
        return ["Metrics unavailable"]
    try:
        if start_time is not None:
            metrics.bot_uptime_seconds.set(time.monotonic() - start_time)
        if products_tracked is not None:
            metrics.products_tracked_total.set(int(products_tracked))
        uptime = metrics.bot_uptime_seconds._value.get()
        tracked = metrics.products_tracked_total._value.get()
    except (AttributeError, TypeError):
        return ["Metrics unavailable"]
    return [
        "<b>📡 Bot Status</b>",
        f"Uptime: {_format_uptime(float(uptime or 0))}",
        f"Products tracked: {int(tracked or 0)}",
    ]


def _tier_label(state: str) -> str:
    """Map a QuarantineState value to a short human-readable tier label."""
    from price_tracker.core.health import QuarantineState  # noqa: PLC0415

    return {
        QuarantineState.LOCKED_T1.value: "T1 (1h)",
        QuarantineState.LOCKED_T2.value: "T2 (6h)",
        QuarantineState.LOCKED_T3.value: "T3 (24h)",
        QuarantineState.HALF_OPEN_T1.value: "T1 half-open",
        QuarantineState.HALF_OPEN_T2.value: "T2 half-open",
        QuarantineState.HALF_OPEN_T3.value: "T3 half-open",
    }.get(state, state)


@with_locale
@admin_only
async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Debug scraping for a URL — shows what each strategy finds."""
    if not context.args:
        await update.message.reply_text(_("❌ Usage: /debug <url>"), parse_mode=ParseMode.HTML)
        return

    url = context.args[0]
    msg = await update.message.reply_text(_("🔍 Analysis in progress..."))

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
                lines.append(
                    _("⚠️ Small response with no structured data, trying a fresh client...")
                )
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
                            lines.append(_("✅ The fresh client got more data!"))
                except Exception as e:  # noqa: BLE001 — debug surface, never crash
                    lines.append(f"❌ httpx fresh: {str(e)[:60]}")
        elif resp.status_code == 403:
            lines.append(_("⚠️ 403 — trying curl_cffi..."))
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
                lines.append(_("⚠️ Trying Scrapling..."))
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
        lines.append(_("\n❌ Could not load the page with any method."))
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
                lines.append(_("📦 JSON-LD: ❌ BS4 finds no scripts, but the regex does!"))
                break
            except _json.JSONDecodeError:
                pass

    if scripts:
        for i, s in enumerate(scripts[:3]):
            try:
                raw = s.string or s.get_text(strip=True)
                if not raw:
                    lines.append(_("📦 JSON-LD #{n}: empty content").format(n=i + 1))
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
        lines.append(_("📦 JSON-LD: ❌ not found"))

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
        lines.append(_("🏷 OG/meta price: ❌ not found"))

    # Step 4: Check microdata
    itemprop_price = soup.find(attrs={"itemprop": "price"})
    if itemprop_price:
        val = itemprop_price.get("content") or itemprop_price.get_text(strip=True)
        lines.append(f"🔖 itemprop=price: <b>{str(val)[:30]}</b>")
    else:
        lines.append(_("🔖 itemprop=price: ❌ not found"))

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
        lines.append(_("🎯 CSS selectors: ❌ no match"))

    # Step 6: Regex price in first 3000 chars of body
    body = soup.find("body")
    if body:
        text = body.get_text(separator=" ")[:3000]
        price_matches = _re.findall(r"€\s*\d+[.,]\d{2}|\d+[.,]\d{2}\s*€", text)
        if price_matches:
            lines.append(f"🔎 Regex €: {', '.join(price_matches[:5])}")
        else:
            lines.append(_("🔎 Regex €: ❌ no match"))

    # Step 7: Title
    title = soup.find("title")
    if title and title.string:
        lines.append(f"\n📝 Title: {_escape_html(title.string.strip()[:80])}")

    # Step 8: Run actual scraper
    scraper = _scraper(context)
    scraper_for_url = scraper.resolve(url)
    lines.append(_("\n🤖 <b>Scraper result:</b>"))
    if scraper_for_url is None:
        lines.append(_("   ❌ no known scraper for this domain"))
    else:
        result = await scraper_for_url.scrape(url, client)
        lines.append(_("   Name: {name}").format(name=_escape_html((result.name or "❌")[:60])))
        price_repr = "€" + str(result.price) if result.price else "❌ " + (result.error or "")
        lines.append(_("   Price: {price}").format(price=price_repr))

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@with_locale
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
        _("📊 <b>Your stats</b>\n"),
        _("📦 Active products: {n}").format(n=user_stats["active_products"]),
        _("📁 Total products: {n}").format(n=user_stats["total_products"]),
        _("🔍 Checks run: {n}").format(n=user_stats["total_checks"]),
        _("⏱ Check interval: every {interval}").format(interval=interval_str),
    ]

    products_tracked: int | None = None
    if is_admin:
        global_stats = await db.get_stats()
        users = await db.get_all_users()
        products_tracked = int(global_stats["active_products"])
        lines.extend(
            [
                "",
                _("<b>👑 Admin overview</b>"),
                _("👥 Active users: {n}").format(n=len(users)),
                _("📦 Total products (global): {n}").format(n=global_stats["active_products"]),
                _("🔍 Total checks (global): {n}").format(n=global_stats["total_checks"]),
            ]
        )

    metrics = context.bot_data.get("metrics")
    start_time = context.bot_data.get("start_time")
    if metrics is not None:
        lines.append("")
        lines.extend(
            _render_metrics_lines(
                metrics,
                start_time=start_time,
                products_tracked=products_tracked,
            )
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render a metrics-only snapshot (uptime + products tracked).

    Slim handler that reads only the metrics registry — suitable for
    out-of-band uses where DB/admin context is not available. The full
    user-stats command remains `cmd_status` (registered as /status, /stato).
    """
    metrics = context.bot_data.get("metrics")
    start_time = context.bot_data.get("start_time")
    lines = ["📊 <b>Bot Status</b>", ""]
    lines.extend(_render_metrics_lines(metrics, start_time=start_time))
    await update.message.reply_html("\n".join(lines))


@with_locale
@admin_only
async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: show scraper health report (English output)."""
    from price_tracker.core.health import QuarantineState  # noqa: PLC0415

    health_mgr = context.bot_data["health_manager"]
    records = health_mgr.all_records()

    # Classify by EFFECTIVE state: an expired lockout is HALF_OPEN on read
    # even though the persisted row still says LOCKED_* (bug #60).
    effective = {r.domain: health_mgr.state(r.domain) for r in records}
    locked_states = (
        QuarantineState.LOCKED_T1,
        QuarantineState.LOCKED_T2,
        QuarantineState.LOCKED_T3,
    )
    half_open_states = (
        QuarantineState.HALF_OPEN_T1,
        QuarantineState.HALF_OPEN_T2,
        QuarantineState.HALF_OPEN_T3,
    )
    healthy = [r for r in records if effective[r.domain] == QuarantineState.CLOSED]
    locked = [r for r in records if effective[r.domain] in locked_states]
    half_open = [r for r in records if effective[r.domain] in half_open_states]

    lines: list[str] = ["🏥 <b>Scraper Health Report</b>", ""]
    lines.append(f"✅ Healthy domains: {len(healthy)}")
    lines.append(f"⚠️ Half-open: {len(half_open)}")
    lines.append(f"🔒 Locked: {len(locked)}")
    lines.append("")

    if locked:
        lines.append("<b>Locked:</b>")
        for r in sorted(locked, key=lambda x: x.locked_until or datetime.max.replace(tzinfo=UTC)):
            lines.append(
                f"  • {r.domain} — {_tier_label(effective[r.domain].value)}, "
                f"expires in {_format_remaining(r.locked_until)}"
            )
        lines.append("")

    if half_open:
        lines.append("<b>Half-open:</b>")
        for r in half_open:
            lines.append(f"  • {r.domain} — probing on next tick")
        lines.append("")

    recent_blocks = sorted(
        (r for r in records if r.last_block_at),
        key=lambda x: x.last_block_at,
        reverse=True,
    )[:5]
    if recent_blocks:
        lines.append("<b>Last 5 block events:</b>")
        for r in recent_blocks:
            ts = r.last_block_at.strftime("%Y-%m-%d %H:%M:%SZ") if r.last_block_at else "—"
            lines.append(f"  • {r.domain} — {r.last_block_reason or '?'} — {ts}")

    await update.message.reply_html("\n".join(lines))


@with_locale
@restricted
async def cmd_errori(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User-facing: products with recent scrape errors + domain quarantine state.

    Complements the admin-only /health (domain-level) with a per-product,
    localized view that surfaces the persisted ``last_error`` for debugging.
    """
    from price_tracker.core.health import QuarantineState  # noqa: PLC0415

    db = _db(context)
    user_id = update.effective_user.id
    health_mgr = context.bot_data.get("health_manager")

    errored = await db.list_products_with_errors(user_id=user_id)
    if not errored:
        await update.message.reply_text(_("✅ No recent errors on your products."))
        return

    lines: list[str] = [
        _("⚠️ <b>Recent errors ({count})</b>").format(count=len(errored)),
        "",
    ]
    for row in errored:
        name = _escape_html((row.name or _("Unknown"))[:50])
        lines.append(f"<b>#{row.id}</b> {name}")

        state_label = ""
        if health_mgr is not None and row.domain:
            state = health_mgr.state(row.domain)
            if state != QuarantineState.CLOSED:
                until = _format_remaining(health_mgr.locked_until(row.domain))
                state_label = _(" — 🔒 {tier} (resumes in {until})").format(
                    tier=_tier_label(state.value), until=until
                )
        lines.append(f"  🌐 {_escape_html(row.domain or '?')}{state_label}")
        lines.append(_("  ❌ {n} failed reads").format(n=row.consecutive_errors))
        if row.last_error:
            when = _format_relative_time(row.last_error_at)
            when_str = f" — {when}" if when else ""
            lines.append(f"  🐞 {_escape_html(row.last_error[:140])}{when_str}")
        lines.append("")

    lines.append(
        _(
            "ℹ️ Sites in 🔒 quarantine resume on their own; "
            "use /reactivate to bring a paused product back."
        )
    )
    await update.message.reply_html("\n".join(lines))


def register(app: Application) -> None:
    """Register debug/status command handlers on `app`."""
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("stato", cmd_status))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("errori", cmd_errori))
    app.add_handler(CommandHandler("errors", cmd_errori))
