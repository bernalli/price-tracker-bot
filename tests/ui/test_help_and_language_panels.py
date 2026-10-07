"""The /help screen: the registry rendered by area, with the admin area for admins only."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from price_tracker.bot.callbacks import Action, InvalidCallback, decode
from price_tracker.bot.commands import COMMANDS
from price_tracker.bot.messages import set_locale
from price_tracker.bot.ui.panels import help_screen
from price_tracker.core.textlimits import SAFE_LIMIT, _is_valid_telegram_markup, visible_length

if TYPE_CHECKING:
    from pathlib import Path

LOCALES = ("en", "it", "zh_Hans", "fr", "es", "de", "uk", "pt_BR", "ja")
LINE = re.compile(r"^/(\w+) — ", re.MULTILINE)


def _listed(text: str) -> set[str]:
    return set(LINE.findall(text))


def test_a_user_sees_every_user_command_and_no_admin_one(ui_locales: Path) -> None:
    set_locale("en")
    text = help_screen(False).text
    assert _listed(text) == {spec.name for spec in COMMANDS if not spec.admin}


def test_an_admin_also_sees_the_admin_commands(ui_locales: Path) -> None:
    set_locale("en")
    text = help_screen(True).text
    assert _listed(text) == {spec.name for spec in COMMANDS}


def test_each_line_carries_the_description_in_the_current_language(ui_locales: Path) -> None:
    set_locale("it")
    text = help_screen(False).text
    set_locale("en")
    assert text != help_screen(False).text
    assert "/list — " in text


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("admin", [False, True])
def test_the_screen_fits_and_is_valid_html_in_every_locale(
    ui_locales: Path, locale: str, admin: bool
) -> None:
    set_locale(locale)
    screen = help_screen(admin)
    assert 0 < visible_length(screen.text) <= SAFE_LIMIT
    assert _is_valid_telegram_markup(screen.text)


def test_the_only_button_goes_home(ui_locales: Path) -> None:
    set_locale("en")
    wires = [button.callback for row in help_screen(False).rows for button in row]
    assert wires == ["h"]
    decoded = decode(wires[0] or "")
    assert not isinstance(decoded, InvalidCallback)
    assert decoded == Action("home")
