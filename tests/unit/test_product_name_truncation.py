"""Product names cut for a Telegram message end in an ellipsis, never mid-word silently.

Handlers used to slice names with ``name[:80]`` and friends: the message ended
in the middle of a word with nothing to say the text was shortened, so the user
read it as a truncated message. Every cut now goes through
``truncate_to_width``: it appends ``…`` only when it cuts, keeps the site's
budget, and runs before HTML escaping so an entity is never split.
"""

from __future__ import annotations

import html
import io
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from price_tracker.bot.callbacks import Action
from price_tracker.bot.handlers import history, monitoring, product
from price_tracker.bot.handlers.callbacks import _actions, _menu, _ops, _product
from price_tracker.bot.handlers.debug import cmd_errori
from price_tracker.bot.ui.width import display_width, truncate_to_width
from price_tracker.core.textlimits import NAME_BUDGET
from price_tracker.db.models import ProductErrorRow

LONG_NAME = (
    "Apple AirPods Pro 3, Cancellazione attiva del rumore, "
    "Rilevamento della frequenza cardiaca, Audio"
)
SHORT_NAME = "Cuffie Pro"
USER_ID = 7
PRODUCT_ID = 1
TAG = re.compile(r"</?(?:b|i|code)>")


class Surfaces:
    """Everything one handler call showed the user: message texts, captions, buttons."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.buttons: list[str] = []
        self.message = MagicMock()

    def record(self, *args: Any, **kwargs: Any) -> MagicMock:
        for value in (*args[:1], kwargs.get("text"), kwargs.get("caption")):
            if isinstance(value, str):
                self.texts.append(value)
        markup = kwargs.get("reply_markup")
        if markup is not None:
            for row in markup.inline_keyboard:
                self.buttons.extend(button.text for button in row)
        return self.message

    def visible(self) -> str:
        """The text as a client shows it: tags dropped, entities decoded."""
        return html.unescape(TAG.sub("", "\n".join([*self.texts, *self.buttons])))

    def raw(self) -> str:
        return "\n".join(self.texts)


def _row(name: str) -> dict[str, Any]:
    return {
        "id": PRODUCT_ID,
        "user_id": USER_ID,
        "name": name,
        "url": "https://shop.example/p/1",
        "current_price": "50.00",
        "initial_price": "60.00",
        "target_price": None,
        "is_active": 1,
        "currency": "EUR",
        "threshold_type": "percentage",
        "threshold_value": "10",
        "consecutive_errors": 0,
    }


def _env(name: str, *, active: bool = True) -> tuple[Surfaces, MagicMock, MagicMock, MagicMock]:
    """A fake update, context and query wired to one product called ``name``."""
    surfaces = Surfaces()
    message = MagicMock()
    for method in ("reply_text", "reply_html", "reply_photo", "edit_text", "edit_message_text"):
        setattr(message, method, AsyncMock(side_effect=surfaces.record))
    message.delete = AsyncMock()
    surfaces.message = message

    row = _row(name)
    row["is_active"] = 1 if active else 0
    db = AsyncMock()
    db.is_user_allowed.return_value = True
    db.is_user_admin.return_value = False
    db.get_product.return_value = row
    db.get_product_for_user.return_value = row
    db.get_active_products.return_value = [row] if active else []
    db.get_all_products.return_value = [row]
    db.delete_product.return_value = True
    db.reset_initial_price.return_value = True
    db.get_config.return_value = None

    scheduler = MagicMock()
    scheduler.check_one_product_for_user = AsyncMock(
        return_value=SimpleNamespace(alert=None, reason=None)
    )
    scheduler.check_user_products_for_user = AsyncMock(return_value=[])
    scheduler.check_products_for_user = AsyncMock(return_value=[])

    context = MagicMock()
    context.bot_data = {
        "db": db,
        "scheduler": scheduler,
        "config": SimpleNamespace(check_interval_minutes=60),
    }
    context.args = []

    update = MagicMock()
    update.effective_user.id = USER_ID
    update.effective_user.language_code = "it"
    update.message = message

    query = MagicMock()
    query.message = message
    query.from_user.id = USER_ID
    query.edit_message_text = AsyncMock(side_effect=surfaces.record)
    update.callback_query = query
    return surfaces, update, context, query


Driver = Callable[[str], Awaitable[Surfaces]]


async def _command(handler: Any, args: list[str], name: str, *, active: bool = True) -> Surfaces:
    surfaces, update, context, _query = _env(name, active=active)
    context.args = args
    await handler(update, context)
    return surfaces


def _command_driver(handler: Any, args: list[str], *, active: bool = True) -> Driver:
    async def drive(name: str) -> Surfaces:
        return await _command(handler, args, name, active=active)

    return drive


def _callback_driver(handler: Any, data: str) -> Driver:
    async def drive(name: str) -> Surfaces:
        surfaces, _update, context, query = _env(name)
        await handler(query, context, context.bot_data["db"], USER_ID, data)
        return surfaces

    return drive


async def _drive_checkall(name: str) -> Surfaces:
    surfaces, update, context, _query = _env(name)
    await monitoring.cmd_checkall(update, context)
    return surfaces


async def _drive_menu_checkall(name: str) -> Surfaces:
    surfaces, _update, context, query = _env(name)
    await _menu.handle_menu_navigation(
        query, context, context.bot_data["db"], USER_ID, "menu_checkall"
    )
    return surfaces


def _menu_driver(data: str, *, active: bool = True) -> Driver:
    async def drive(name: str) -> Surfaces:
        surfaces, _update, context, query = _env(name, active=active)
        await _menu.handle_menu_navigation(query, context, context.bot_data["db"], USER_ID, data)
        return surfaces

    return drive


async def _drive_errori(name: str) -> Surfaces:
    surfaces, update, context, _query = _env(name)
    row = ProductErrorRow(
        id=PRODUCT_ID,
        name=name,
        url="https://shop.example/p/1",
        domain=None,
        consecutive_errors=2,
        last_error="boom",
        last_error_at=None,
    )
    context.bot_data["db"].list_products_with_errors.return_value = [row]
    await cmd_errori(update, context)
    return surfaces


async def _drive_history_caption(name: str) -> Surfaces:
    surfaces, update, context, _query = _env(name)
    context.args = ["1"]
    with patch.object(history, "_generate_chart", AsyncMock(return_value=io.BytesIO(b"png"))):
        await history.cmd_history(update, context)
    return surfaces


async def _drive_chart_button(name: str) -> Surfaces:
    surfaces, _update, context, query = _env(name)
    with patch.object(_product, "_generate_chart", AsyncMock(return_value=io.BytesIO(b"png"))):
        await _product.handle_chart_button(
            query, context, context.bot_data["db"], USER_ID, "chart_1"
        )
    return surfaces


async def _drive_ops_reactivate(name: str) -> Surfaces:
    class Suspended(dict[str, Any]):
        id = PRODUCT_ID

    surfaces, _update, context, query = _env(name)
    db = context.bot_data["db"]
    db.list_auto_suspended_products.return_value = [Suspended(_row(name))]
    await _ops._handle_reactivate(query, context, db, USER_ID, Action("ops.reactivate", (1,)))
    return surfaces


async def _drive_add_product(name: str) -> Surfaces:
    surfaces, update, context, _query = _env(name)
    db = context.bot_data["db"]
    db.get_product_by_url_for_user.return_value = None
    db.add_product.return_value = PRODUCT_ID
    scraper = AsyncMock()
    scraper.scrape.return_value = SimpleNamespace(
        price=Decimal("10.00"), name=name, currency="EUR", error=None
    )
    context.bot_data["scraper"] = MagicMock()
    context.bot_data["scraper"].resolve.return_value = scraper
    context.bot_data["http_client"] = MagicMock()
    with patch("price_tracker.core.url_utils.validate_public_url", MagicMock()):
        await product._add_product(update, context, "https://shop.example/p/1")
    return surfaces


async def _drive_flow_product_name(name: str) -> Surfaces:
    from price_tracker.bot.flow_services import RepositoryFlowServices

    surfaces = Surfaces()
    store = AsyncMock()
    store.is_user_admin.return_value = False
    store.get_product_for_user.return_value = _row(name)
    shown = await RepositoryFlowServices(lambda: store).product_name(USER_ID, PRODUCT_ID)
    surfaces.texts.append(shown or "")
    return surfaces


async def _drive_chart_title(name: str) -> Surfaces:
    surfaces, _update, context, _query = _env(name)
    row = _row(name)
    db = context.bot_data["db"]
    db.get_price_history.return_value = [
        {"price": "50.00", "checked_at": "2026-06-01 10:00:00"},
        {"price": "40.00", "checked_at": "2026-06-02 10:00:00"},
    ]
    captured: list[str] = []

    def fake_render(dates: Any, prices: Any, target: Any, title: str) -> io.BytesIO:
        captured.append(title)
        return io.BytesIO(b"png")

    with patch.object(history, "_render_chart", fake_render):
        await history._generate_chart(db, PRODUCT_ID, row)
    surfaces.texts.extend(captured)
    return surfaces


# (id, driver, remaining display cells for the name in this fixture)
SITES: list[tuple[str, Driver, int]] = [
    ("cmd_reactivate", _command_driver(monitoring.cmd_reactivate, ["1"], active=False), 80),
    ("cmd_pause", _command_driver(monitoring.cmd_pause, ["1"]), 80),
    ("cmd_refresh_reset", _command_driver(monitoring.cmd_refresh, ["1", "0"]), 80),
    ("cmd_refresh_set", _command_driver(monitoring.cmd_refresh, ["1", "30"]), 80),
    ("cmd_check", _command_driver(monitoring.cmd_check, ["1"]), 80),
    ("cmd_checkall", _drive_checkall, 17),
    ("monitoring_picker", _command_driver(monitoring.cmd_check, []), 17),
    ("reactivate_picker", _command_driver(monitoring.cmd_reactivate, [], active=False), 14),
    ("cmd_delete_confirm", _command_driver(product.cmd_delete, ["1"]), 80),
    ("cmd_delete_picker", _command_driver(product.cmd_delete, []), 17),
    ("cmd_target_picker", _command_driver(product.cmd_target, []), 17),
    ("cmd_threshold_picker", _command_driver(product.cmd_threshold, []), 17),
    ("cmd_target", _command_driver(product.cmd_target, ["1", "10"]), 80),
    ("cmd_threshold", _command_driver(product.cmd_threshold, ["1", "20%"]), 80),
    ("cmd_reset", _command_driver(history.cmd_reset, ["1"]), 60),
    ("history_picker", _command_driver(history.cmd_history, []), 17),
    ("history_caption", _drive_history_caption, 50),
    ("chart_title", _drive_chart_title, 50),
    ("cmd_errori", _drive_errori, 24),
    ("edit_button", _callback_driver(_actions.handle_edit_button, "edit_1"), 60),
    ("pause_button", _callback_driver(_actions.handle_pause_button, "pause_1"), NAME_BUDGET),
    ("remove_button", _callback_driver(_actions.handle_remove_button, "remove_1"), 50),
    ("reset_button", _callback_driver(_actions.handle_reset_button, "reset_1"), 60),
    (
        "reactivate_button",
        _callback_driver(_actions.handle_reactivate_button, "reactivate_1"),
        NAME_BUDGET,
    ),
    ("confirm_delete", _callback_driver(_product.handle_delete_flow, "confirm_delete_1"), 60),
    ("check_button", _callback_driver(_product.handle_check_button, "check_1"), 60),
    ("chart_button", _drive_chart_button, 50),
    ("amazon_pref", _callback_driver(_product.handle_amazon_pref, "pref_new_1"), 60),
    ("track_any", _callback_driver(_product.handle_track_choice, "track_any_1"), 60),
    ("track_default", _callback_driver(_product.handle_track_choice, "track_default_1"), 60),
    ("menu_checkall", _drive_menu_checkall, 18),
    ("menu_prodotti", _menu_driver("menu_prodotti"), 18),
    ("menu_paused", _menu_driver("menu_paused", active=False), 15),
    ("menu_prezzi", _menu_driver("menu_prezzi"), 18),
    ("menu_storia", _menu_driver("menu_storia"), 18),
    ("menu_notifiche", _menu_driver("menu_notifiche"), 18),
    ("ops_reactivate", _drive_ops_reactivate, 15),
    ("add_product", _drive_add_product, 80),
    ("flow_product_name", _drive_flow_product_name, 60),
]
SITE_IDS = [site[0] for site in SITES]
# These surfaces carry plain text (a chart title, a flow prompt), not HTML.
PLAIN_TEXT_SITES = frozenset({"chart_title", "flow_product_name"})


def test_fixture_is_longer_than_every_budget() -> None:
    assert display_width(LONG_NAME) > max(budget for _id, _driver, budget in SITES)


@pytest.mark.parametrize(("site", "driver", "budget"), SITES, ids=SITE_IDS)
async def test_long_name_is_cut_with_an_ellipsis_at_the_site_budget(
    site: str, driver: Driver, budget: int
) -> None:
    shown = (await driver(LONG_NAME)).visible()

    expected = truncate_to_width(LONG_NAME, budget)
    assert expected.endswith("…")
    assert LONG_NAME.startswith(expected[:-1])
    assert expected in shown, f"{site}: {shown!r}"
    # Nothing beyond the budget leaks through after the ellipsis.
    assert LONG_NAME[: len(expected)] not in shown


@pytest.mark.parametrize(("site", "driver", "budget"), SITES, ids=SITE_IDS)
async def test_short_name_is_shown_whole_without_an_ellipsis(
    site: str, driver: Driver, budget: int
) -> None:
    shown = (await driver(SHORT_NAME)).visible()

    assert SHORT_NAME in shown, f"{site}: {shown!r}"
    assert f"{SHORT_NAME}…" not in shown
    assert f"{SHORT_NAME}..." not in shown


@pytest.mark.parametrize(("site", "driver", "budget"), SITES, ids=SITE_IDS)
async def test_name_with_special_characters_at_the_cut_stays_valid_html(
    site: str, driver: Driver, budget: int
) -> None:
    # The cut falls right after "&" and a "<" follows: slicing the escaped text
    # would leave a dangling "&am" and let a raw "<" through.
    name = "A" * (budget - 2) + "&<b>x</b> and the rest of a very long product name"
    surfaces = await driver(name)

    if site not in PLAIN_TEXT_SITES:
        markup = TAG.sub("", surfaces.raw())
        assert not re.search(r"&(?!(?:amp|lt|gt);)", markup), f"{site}: broken entity in {markup!r}"
        assert "<" not in markup.replace("&lt;", "")
    expected = truncate_to_width(name, budget)
    assert expected in surfaces.visible()
    assert expected.endswith("…")
