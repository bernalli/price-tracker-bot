"""Auth decorators and shared `bot_data` accessors.

Ported from monolithic bot.py [item 17].

`_db`, `_config`, `_scraper`, `_client` resolve runtime dependencies stashed
into `application.bot_data` at startup; the bootstrap glue lives outside this
module (Phase 3 — item 19) so handlers stay framework-agnostic.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from decimal import Decimal
from functools import wraps
from typing import TYPE_CHECKING, Any

from telegram.constants import ParseMode

from price_tracker.bot.messages import set_locale

if TYPE_CHECKING:
    from collections.abc import Awaitable

    import httpx
    from telegram import Update
    from telegram.ext import ContextTypes


HandlerFn = Callable[..., "Awaitable[Any]"]


def with_locale(handler: HandlerFn) -> HandlerFn:
    """Decorator that sets the request locale from Telegram update before
    invoking the wrapped handler.

    Reads `update.effective_user.language_code` and falls through the
    fallback chain in `bot.messages.get_translation`. ContextVar isolates
    locale across concurrent asyncio tasks.

    MUST be applied as the OUTERMOST decorator so error replies from
    `@restricted` / `@admin_only` are already localized.
    """

    @wraps(handler)
    async def wrapper(update: Any, context: Any, *args: Any, **kwargs: Any) -> Any:
        lang = update.effective_user.language_code if update.effective_user is not None else None
        set_locale(lang)
        return await handler(update, context, *args, **kwargs)

    return wrapper


def restricted(func: HandlerFn) -> HandlerFn:
    """Restrict a handler to authorized users (checked via DB)."""

    @wraps(func)
    async def wrapped(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any
    ) -> Any:
        user = update.effective_user
        if not user:
            return None
        db = _db(context)
        allowed = await db.is_user_allowed(user.id)
        if not allowed:
            if update.message:
                await update.message.reply_text(
                    f"⛔ Non sei autorizzato.\n"
                    f"Il tuo ID Telegram è: <code>{user.id}</code>\n\n"
                    f"Chiedi all'amministratore di aggiungerti con:\n"
                    f"<code>/adduser {user.id}</code>",
                    parse_mode=ParseMode.HTML,
                )
            return None
        # Save user display name (best-effort; ignored on failure)
        with contextlib.suppress(Exception):
            await db.update_user_info(
                user.id,
                display_name=user.first_name or user.full_name,
                username=user.username,
            )
        return await func(update, context, *args, **kwargs)

    return wrapped


def admin_only(func: HandlerFn) -> HandlerFn:
    """Restrict a handler to admin users only."""

    @wraps(func)
    async def wrapped(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any
    ) -> Any:
        user = update.effective_user
        if not user:
            return None
        db = _db(context)
        is_admin = await db.is_user_admin(user.id)
        if not is_admin:
            if update.message:
                await update.message.reply_text(
                    "⛔ Solo l'amministratore può usare questo comando."
                )
            return None
        return await func(update, context, *args, **kwargs)

    return wrapped


# ── bot_data accessors ───────────────────────────────────────────


def _db(ctx: ContextTypes.DEFAULT_TYPE) -> Any:
    """Return the DB / repository handle stashed by the bootstrap layer."""
    return ctx.bot_data["db"]


def _config(ctx: ContextTypes.DEFAULT_TYPE) -> Any:
    """Return the runtime Config instance."""
    return ctx.bot_data["config"]


def _scraper(ctx: ContextTypes.DEFAULT_TYPE) -> Any:
    """Return the ScraperManager / registry instance."""
    return ctx.bot_data["scraper"]


def _client(ctx: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    """Return the shared async HTTP client."""
    return ctx.bot_data["http_client"]


# ── Currency conversion helpers (kept here per item 17 mapping) ──

_FALLBACK_RATES: dict[str, Decimal] = {
    "CHF": Decimal("0.94"),
    "USD": Decimal("0.92"),
    "GBP": Decimal("1.18"),
    "SEK": Decimal("0.087"),
    "NOK": Decimal("0.085"),
    "DKK": Decimal("0.13"),
    "PLN": Decimal("0.23"),
    "CZK": Decimal("0.041"),
    "JPY": Decimal("0.006"),
}

# Populated externally by `core/currency.py` (refreshed on the scheduler).
_ECB_RATES: dict[str, Decimal] = {}


def _get_conversion_rate(currency: str) -> Decimal | None:
    """Return the latest known EUR conversion rate for `currency`."""
    if currency in _ECB_RATES:
        return _ECB_RATES[currency]
    return _FALLBACK_RATES.get(currency)


def _convert_display(price: Decimal, currency: str) -> str:
    """Format `price`+`currency` for display, appending an EUR estimate if non-EUR."""
    rate = _get_conversion_rate(currency)
    if rate:
        eur = (price * rate).quantize(Decimal("0.01"))
        return f"{currency} {price:.2f} (~€{eur:.2f})"
    return f"€{price:.2f}"
