"""The /help screen and the language section of the settings.

/help renders the command registry by area, with the admin area for admins only.
The language section offers Automatic and every language with a catalogue, the
stored choice marked.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from price_tracker.bot.callbacks import Action, InvalidCallback, decode
from price_tracker.bot.commands import COMMANDS
from price_tracker.bot.messages import set_locale
from price_tracker.bot.ui.panels import help_screen, settings_screen, settings_section_screen
from price_tracker.core.textlimits import SAFE_LIMIT, _is_valid_telegram_markup, visible_length
from tests.support.panel_variants import BUSY, NOW

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


# --- the language section ------------------------------------------------------


def _checked(language: str | None) -> list[str]:
    screen = settings_section_screen("lang", BUSY, now=NOW, language=language)
    return [btn.callback or "" for row in screen.rows for btn in row if btn.label.endswith("✓")]


@pytest.mark.parametrize(
    ("stored", "checked"),
    [(None, "s:lang:auto"), ("en", "s:lang:en"), ("it", "s:lang:it"), ("xx", "s:lang:auto")],
)
def test_the_stored_choice_carries_the_only_check_mark(
    ui_locales: Path, stored: str | None, checked: str
) -> None:
    set_locale("en")
    assert _checked(stored) == [checked]


def test_the_presets_are_automatic_then_each_language_in_its_own_name(ui_locales: Path) -> None:
    set_locale("en")
    screen = settings_section_screen("lang", BUSY, now=NOW, language=None)
    labels = [btn.label for row in screen.rows for btn in row]
    assert labels == ["Automatic ✓", "English", "Italiano", "◀️ Settings"]
    for row in screen.rows:
        for btn in row:
            assert not isinstance(decode(btn.callback or ""), InvalidCallback)


@pytest.mark.parametrize(
    ("stored", "locale", "current"),
    [
        (None, "en", "Current: Automatic (English)"),
        (None, "it", "Attuale: Automatica (Italiano)"),
        ("it", "en", "Current: Italiano"),
        ("en", "it", "Attuale: English"),
    ],
)
def test_the_section_names_the_current_language(
    ui_locales: Path, stored: str | None, locale: str, current: str
) -> None:
    set_locale(locale)
    text = settings_section_screen("lang", BUSY, now=NOW, language=stored).text
    assert text.endswith(current)


def test_the_overview_shows_the_language_and_offers_its_section(ui_locales: Path) -> None:
    set_locale("en")
    screen = settings_screen(BUSY, now=NOW, language="it")
    assert "🗣 Language: Italiano" in screen.text
    wires = [btn.callback for row in screen.rows for btn in row]
    assert "s:lang" in wires
    automatic = settings_screen(BUSY, now=NOW, language=None).text
    assert "🗣 Language: Automatic (English)" in automatic
