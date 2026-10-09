"""The Admin, Alert rule, Notifications, Status & info and error-report panels follow the
reply language: English text in ``en``, today's Italian in ``it``, the same buttons in both.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers import _menu_back_button
from price_tracker.bot.handlers._helpers import _format_threshold
from price_tracker.bot.handlers.callbacks._actions import handle_edit_button
from price_tracker.bot.handlers.callbacks._admin import handle_admin_menu
from price_tracker.bot.handlers.callbacks._menu import handle_menu_navigation
from price_tracker.bot.handlers.debug import errors_text, no_errors_text
from price_tracker.bot.keyboards import menu_back_button
from price_tracker.bot.messages import reset_locale, set_locale
from price_tracker.core.health import QuarantineState
from price_tracker.db.models import ProductErrorRow

# Same audit as the navigation catalog test, plus any accented letter.
_ENGLISH_AUDIT = re.compile(
    r"[àèéìòù]|\b(prezzo|errore|comando|impostazion|aggiungere|elenca|notifica|riprova|sono)\b"
)
_ACCENTED = re.compile(r"[À-ÿ]")
# The Italian words these panels used before they followed the reply language.
_ITALIAN_WORDS = re.compile(
    r"(?i)\b(Impostazioni|Utent[ei]|attiv[io]|Intervallo|global[ei]|Lista|Aggiungi|Rimuovi|Modifica"
    r"|Soglia|attuale|Prezzo|Cosa|vuoi|modificare|Notifiche|Tocca|cambiare|Statistiche"
    r"|Prodott[io]|Totali|ogni|Ogni|ribasso|Errori|recenti|letture|fallite|riprende|tra|siti"
    r"|quarantena|riattivare|Sconosciuto|impostato|Azzera|Nessun|tuoi|trovato|valido)\b"
)
USER = 10


@contextmanager
def _locale(code: str) -> Iterator[None]:
    token = set_locale(code)
    try:
        yield
    finally:
        reset_locale(token)


def _product(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": 1,
        "name": "Kettle",
        "threshold_type": "any_drop",
        "threshold_value": "0",
        "target_price": None,
        "initial_price": "100",
        "current_price": "80",
    }
    record.update(overrides)
    return record


def _db(*, product: dict[str, Any] | None = None, is_admin: bool = False) -> MagicMock:
    db = MagicMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=is_admin)
    db.get_product = AsyncMock(return_value=product)
    db.get_product_for_user = AsyncMock(return_value=product)
    db.get_active_products = AsyncMock(return_value=[product] if product else [])
    db.list_active_users = AsyncMock(return_value=[{"user_id": USER}, {"user_id": 11}])
    db.get_config = AsyncMock(return_value=None)
    db.get_stats = AsyncMock(
        return_value={"active_products": 1, "total_products": 2, "total_checks": 5}
    )
    return db


def _context(db: MagicMock) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"db": db, "config": SimpleNamespace(check_interval_minutes=360)}
    context.user_data = {}
    return context


def _query() -> MagicMock:
    query = MagicMock()
    query.from_user.id = USER
    query.edit_message_text = AsyncMock()
    query.message.reply_text = AsyncMock()
    return query


def _rendered(mock: AsyncMock) -> tuple[str, list[tuple[str, str]]]:
    args: Any = mock.await_args
    text = str(args.args[0])
    markup = args.kwargs.get("reply_markup")
    buttons = (
        []
        if markup is None
        else [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
    )
    return text, buttons


Panel = Callable[[], Awaitable[tuple[str, list[tuple[str, str]]]]]


async def _admin_menu() -> tuple[str, list[tuple[str, str]]]:
    query = _query()
    db = _db(is_admin=True)
    assert await handle_admin_menu(query, _context(db), db, USER, "menu_admin")
    return _rendered(query.edit_message_text)


def _edit(product: dict[str, Any] | None, data: str = "edit_1") -> Panel:
    async def run() -> tuple[str, list[tuple[str, str]]]:
        query = _query()
        db = _db(product=product)
        assert await handle_edit_button(query, _context(db), db, USER, data)
        if query.message.reply_text.await_count:
            return _rendered(query.message.reply_text)
        return _rendered(query.edit_message_text)

    return run


def _menu(data: str, *, is_admin: bool = False) -> Panel:
    async def run() -> tuple[str, list[tuple[str, str]]]:
        query = _query()
        db = _db(product=_product(), is_admin=is_admin)
        assert await handle_menu_navigation(query, _context(db), db, USER, data)
        return _rendered(query.edit_message_text)

    return run


async def _errors() -> tuple[str, list[tuple[str, str]]]:
    db = MagicMock()
    db.list_products_with_errors = AsyncMock(
        return_value=[
            ProductErrorRow(
                id=1,
                name=None,
                url="https://shop.example/p/1",
                domain="shop.example",
                consecutive_errors=3,
                last_error="parse_error: no price",
                last_error_at=None,
            )
        ]
    )
    health = MagicMock()
    health.state = MagicMock(return_value=QuarantineState.LOCKED_T1)
    health.locked_until = MagicMock(return_value=None)
    text = await errors_text(db, health, USER)
    assert text is not None
    return text, []


async def _no_errors() -> tuple[str, list[tuple[str, str]]]:
    return no_errors_text(), []


async def _back_buttons() -> tuple[str, list[tuple[str, str]]]:
    buttons = [*menu_back_button(), *_menu_back_button()]
    return _format_threshold("any_drop", "0"), [(b.text, str(b.callback_data)) for b in buttons]


PANELS: dict[str, Panel] = {
    "admin_menu": _admin_menu,
    "edit": _edit(_product()),
    "edit_not_found": _edit(None),
    "edit_invalid_id": _edit(_product(), "edit_x"),
    "notifications": _menu("menu_notifiche"),
    "info_user": _menu("menu_info"),
    "info_admin": _menu("menu_info", is_admin=True),
    "errors": _errors,
    "no_errors": _no_errors,
    "back_buttons": _back_buttons,
}

# Today's Italian, written out independently of the code.
ITALIAN: dict[str, str] = {
    "admin_menu": ("👑 <b>Impostazioni</b>\n\n👥 Utenti attivi: 2\n⏱ Intervallo globale: 360 min"),
    "edit": (
        "✏️ <b>Modifica #1</b> Kettle\n\n"
        "🎯 Soglia attuale: <b>🔔 Ogni ribasso</b>\n"
        "🏁 Target attuale: <b>non impostato</b>\n"
        "📌 Prezzo base: <b>€100.00</b>\n\n"
        "<b>Cosa vuoi modificare?</b>"
    ),
    "edit_not_found": "❌ Prodotto non trovato.",
    "edit_invalid_id": "❌ ID non valido.",
    "notifications": "🔔 <b>Notifiche</b>\n\nTocca un prodotto per cambiare soglia o target.",
    "info_user": (
        "ℹ️ <b>Statistiche</b>\n\n"
        "📦 Prodotti attivi: 1\n📁 Totali: 2\n🔄 Check: 5\n⏱ Intervallo: ogni 6h"
    ),
    "info_admin": (
        "ℹ️ <b>Statistiche</b>\n\n"
        "📦 Prodotti attivi: 1\n📁 Totali: 2\n🔄 Check: 5\n⏱ Intervallo: ogni 6h\n\n"
        "👑 <b>Admin</b>\n👥 Utenti attivi: 2\n📦 Prodotti globali: 1\n🔄 Check globali: 5"
    ),
    "errors": (
        "⚠️ <b>Errori recenti (1)</b>\n\n"
        "<b>N/D</b> 🔒3 Sconosciuto\n\n"
        "ℹ️ I siti in 🔒 quarantena riprendono da soli; "
        "usa /reactivate per riattivare un prodotto sospeso."
    ),
    "no_errors": "✅ Nessun errore recente sui tuoi prodotti.",
    "back_buttons": "🔔 Ogni ribasso",
}

ITALIAN_LABELS: dict[str, list[str]] = {
    "admin_menu": [
        "👥 Lista utenti",
        "➕ Aggiungi utente",
        "🚫 Rimuovi utente",
        "✏️ Nickname utente",
        "⏱ Intervallo globale: 360 min",
        "🔧 Debug scraper",
        "🏥 Salute scraper",
        "◀️ Menu",
    ],
    "edit": [
        "🔔 Ogni ribasso",
        "📉 Soglia % o €",
        "💰 Prezzo target",
        "🔄 Azzera prezzo base",
        "🔔 Notifiche",
    ],
    "notifications": ["80,00\xa0€ ▼20% Kettle", "◀️ Menu"],
    "back_buttons": ["◀️ Menu", "◀️ Menu"],
}


@pytest.mark.parametrize("panel", sorted(PANELS))
@pytest.mark.asyncio
async def test_english_panel_has_no_italian_and_the_same_buttons(panel: str) -> None:
    with _locale("en"):
        en_text, en_buttons = await PANELS[panel]()
    with _locale("it"):
        _it_text, it_buttons = await PANELS[panel]()
    assert [data for _label, data in en_buttons] == [data for _label, data in it_buttons]
    for shown in (en_text, *(label for label, _data in en_buttons)):
        assert _ENGLISH_AUDIT.search(shown) is None, shown
        assert _ACCENTED.search(shown) is None, shown
        assert _ITALIAN_WORDS.search(shown) is None, shown


@pytest.mark.parametrize("panel", sorted(PANELS))
@pytest.mark.asyncio
async def test_italian_panel_is_unchanged(panel: str) -> None:
    with _locale("it"):
        text, buttons = await PANELS[panel]()
    assert text == ITALIAN[panel]
    if panel in ITALIAN_LABELS:
        assert [label for label, _data in buttons] == ITALIAN_LABELS[panel]


@pytest.mark.asyncio
async def test_hostile_product_name_is_escaped_in_both_languages() -> None:
    for code in ("en", "it"):
        with _locale(code):
            text, _buttons = await _edit(_product(name='<b>&"'))()
        assert '&lt;b&gt;&amp;"' in text
        assert "<b>&" not in text


@pytest.mark.asyncio
async def test_unknown_threshold_type_still_renders() -> None:
    with _locale("en"):
        text, _buttons = await _edit(_product(threshold_type="weird", threshold_value="7"))()
    assert "-€7.00" in text


@pytest.mark.parametrize(
    ("panel", "wire"),
    [
        ("edit", "p:1:pr"),
        ("info_user", "er"),
        ("info_admin", "er"),
        ("admin_menu", "a:hl"),
    ],
)
@pytest.mark.asyncio
async def test_the_panel_links_to_its_new_node(panel: str, wire: str) -> None:
    with _locale("en"):
        _text, buttons = await PANELS[panel]()
    assert wire in [data for _label, data in buttons]
