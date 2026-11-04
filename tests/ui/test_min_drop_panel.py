"""The minimum-drop preference in Settings: its line, its section and its presets."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from price_tracker.app.views import min_drop_view
from price_tracker.bot.callbacks import Action, InvalidCallback, decode, encode
from price_tracker.bot.messages import set_locale
from price_tracker.bot.ui.panels import settings_screen, settings_section_screen
from price_tracker.core.drop_floor import DEFAULT_DROP_FLOOR, DropFloor
from tests.support.panel_variants import BUSY, NOW

if TYPE_CHECKING:
    from collections.abc import Iterator

    from price_tracker.bot.ui.screens import Screen


@pytest.fixture(autouse=True)
def _english() -> Iterator[None]:
    set_locale("en")
    yield
    set_locale("en")


def _buttons(screen: Screen) -> list[tuple[str, Action]]:
    pairs = []
    for row in screen.rows:
        for btn in row:
            decoded = decode(btn.callback)
            assert not isinstance(decoded, InvalidCallback)
            pairs.append((btn.label, decoded))
    return pairs


def _checked(screen: Screen) -> list[str]:
    return [text for text, _action in _buttons(screen) if text.endswith("✓")]


@pytest.mark.parametrize(
    ("choice", "line"),
    [
        (None, "📉 Minimum drop: 2% and 1 (default)"),
        ("off", "📉 Minimum drop: off"),
        ("1", "📉 Minimum drop: 1% and 1"),
        ("5", "📉 Minimum drop: 5% and 1"),
    ],
)
def test_the_overview_shows_the_floor_in_effect(choice: str | None, line: str) -> None:
    text = settings_screen(BUSY, now=NOW, min_drop=min_drop_view(choice)).text
    assert line in text.splitlines()


def test_the_overview_shows_a_configured_default() -> None:
    default = DropFloor(Decimal("2.5"), Decimal(3))
    text = settings_screen(BUSY, now=NOW, min_drop=min_drop_view(None, default)).text
    assert "📉 Minimum drop: 2.5% and 3 (default)" in text.splitlines()


def test_the_overview_leads_to_the_section() -> None:
    actions = [action for _text, action in _buttons(settings_screen(BUSY, now=NOW))]
    assert Action("settings.section", ("md",)) in actions


@pytest.mark.parametrize(
    ("choice", "checked"),
    [(None, ["2% ✓"]), ("off", ["Off ✓"]), ("1", ["1% ✓"]), ("2", ["2% ✓"]), ("5", ["5% ✓"])],
)
def test_the_section_offers_the_presets_and_marks_the_current_one(
    choice: str | None, checked: list[str]
) -> None:
    screen = settings_section_screen("md", BUSY, now=NOW, min_drop=min_drop_view(choice))
    presets = [action for _text, action in _buttons(screen) if action.name == "settings.min_drop"]
    assert presets == [Action("settings.min_drop", (v,)) for v in ("off", "1", "2", "5")]
    assert _checked(screen) == checked


def test_a_default_that_is_no_preset_marks_none() -> None:
    screen = settings_section_screen(
        "md", BUSY, now=NOW, min_drop=min_drop_view(None, DropFloor(Decimal(3), Decimal(1)))
    )
    assert _checked(screen) == []


def test_the_section_explains_both_parts() -> None:
    text = settings_section_screen("md", BUSY, now=NOW, min_drop=min_drop_view("5")).text
    assert text.startswith("📉 <b>Minimum drop</b>")
    assert "at least 1 in the product's currency" in text
    assert text.endswith("Current: 5% and 1")


def test_the_section_is_in_italian() -> None:
    set_locale("it")
    text = settings_section_screen("md", BUSY, now=NOW).text
    assert text.startswith("📉 <b>Calo minimo</b>")
    assert text.endswith("Attuale: 2% e 1 (predefinito)")
    overview = settings_screen(BUSY, now=NOW, min_drop=min_drop_view("off")).text
    assert "📉 Calo minimo: off" in overview.splitlines()


@pytest.mark.parametrize("value", ["off", "1", "2", "5"])
def test_every_preset_round_trips(value: str) -> None:
    action = Action("settings.min_drop", (value,))
    assert decode(encode(action)) == action


@pytest.mark.parametrize("wire", ["s:md:3", "s:md:", "s:md:0", "s:md:default"])
def test_other_values_are_refused(wire: str) -> None:
    assert isinstance(decode(wire), InvalidCallback)


def test_the_default_floor_is_the_documented_one() -> None:
    assert DropFloor(Decimal(2), Decimal(1)) == DEFAULT_DROP_FLOOR


@pytest.mark.parametrize("stored", ["7", "", 3, None])
def test_an_unknown_stored_value_shows_the_default(stored: object) -> None:
    view = min_drop_view(stored)
    assert view.is_default
    assert view.floor == DEFAULT_DROP_FLOOR


def test_the_view_refuses_an_unknown_choice() -> None:
    from price_tracker.app.views import MinDropView

    with pytest.raises(ValueError, match="choice"):
        MinDropView("3", DEFAULT_DROP_FLOOR, Decimal(1))
    with pytest.raises(ValueError, match="floor"):
        MinDropView(None, "2%", Decimal(1))  # type: ignore[arg-type]
