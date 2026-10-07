"""The settings panels: pure screens, in every supported locale."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from price_tracker.app.views import HomeView, PrefsView
from price_tracker.bot.callbacks import Action, InvalidCallback, decode
from price_tracker.bot.messages import set_locale
from price_tracker.bot.ui.panels import home_screen, settings_screen, settings_section_screen
from price_tracker.bot.ui.width import display_width
from price_tracker.core.textlimits import SAFE_LIMIT, _is_valid_telegram_markup, visible_length
from tests.support.panel_variants import BUSY, NOW, SCREENS, prefs
from tests.support.ui_snapshot import compare_or_update, render_snapshot

_SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "panels"
_NOW_TEXT = "2026-03-01T12:00:00Z"
if TYPE_CHECKING:
    from price_tracker.bot.ui.screens import Screen

HALF_WIDTH = 17
ROW_WIDTH = 34
LOCALES = ("en", "it", "zh_Hans", "fr", "es", "de", "uk", "pt_BR", "ja")
HOSTILE = "<b>&\"'\u202e\u200d\n</b>\u2028"


def _callbacks(screen: Screen) -> list[str]:
    return [btn.callback for row in screen.rows for btn in row if btn.callback is not None]


def _actions(screen: Screen) -> list[Action]:
    decoded = [decode(data) for data in _callbacks(screen)]
    assert not any(isinstance(item, InvalidCallback) for item in decoded)
    return [item for item in decoded if isinstance(item, Action)]


def test_main_panel_offers_the_four_sections_and_home() -> None:
    assert _actions(settings_screen(BUSY, now=NOW)) == [
        Action("settings.section", ("mu",)),
        Action("settings.section", ("dg",)),
        Action("settings.section", ("qh",)),
        Action("settings.section", ("lang",)),
        Action("home"),
    ]


@pytest.mark.parametrize(
    ("section", "values", "action"),
    [
        ("mu", ["1", "8", "24", "0", "off"], "settings.mute"),
        ("dg", ["on", "off"], "settings.digest"),
        ("qh", ["2208", "off"], "settings.quiet"),
        ("lang", ["auto", "en", "it"], "settings.language"),
    ],
)
def test_section_offers_its_presets_and_a_way_back(
    section: str, values: list[str], action: str
) -> None:
    actions = _actions(settings_section_screen(section, BUSY, now=NOW))
    assert actions == [*(Action(action, (value,)) for value in values), Action("settings")]


def test_unknown_section_is_rejected() -> None:
    with pytest.raises(ValueError, match="section"):
        settings_section_screen("zz", BUSY, now=NOW)


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError, match="now"):
        settings_screen(BUSY, now=NOW.replace(tzinfo=None))


def test_a_mute_that_already_ended_reads_as_off() -> None:
    ended = prefs(mute=True, mute_until=NOW - timedelta(seconds=1))
    assert settings_screen(ended, now=NOW) == settings_screen(prefs(), now=NOW)
    assert settings_section_screen("mu", ended, now=NOW) == settings_section_screen(
        "mu", prefs(), now=NOW
    )


def test_mute_until_the_exact_instant_reads_as_off() -> None:
    edge = prefs(mute=True, mute_until=NOW)
    assert settings_screen(edge, now=NOW) == settings_screen(prefs(), now=NOW)


def test_a_running_mute_shows_the_local_end_time(ui_locales: Path) -> None:
    set_locale("en")
    text = settings_screen(prefs(mute=True, mute_until=NOW + timedelta(hours=2)), now=NOW).text
    assert "until" in text
    assert "3:00" in text  # 14:00 UTC is 15:00 in Europe/Rome, 3 PM in English


def test_forever_and_off_are_told_apart() -> None:
    forever = settings_screen(prefs(mute=True), now=NOW).text
    off = settings_screen(prefs(), now=NOW).text
    assert "forever" in forever
    assert "forever" not in off


def _checked(screen: Screen) -> list[str]:
    return [btn.callback or "" for row in screen.rows for btn in row if btn.label.endswith("✓")]


def test_the_current_value_carries_the_only_check_mark() -> None:
    assert _checked(settings_section_screen("mu", prefs(), now=NOW)) == ["s:mu:off"]
    assert _checked(settings_section_screen("mu", prefs(mute=True), now=NOW)) == ["s:mu:0"]
    assert _checked(settings_section_screen("dg", prefs(digest_mode=True), now=NOW)) == ["s:dg:on"]
    assert _checked(settings_section_screen("dg", prefs(), now=NOW)) == ["s:dg:off"]
    window = prefs(quiet_hours_start="22:00", quiet_hours_end="08:00")
    assert _checked(settings_section_screen("qh", window, now=NOW)) == ["s:qh:2208"]
    assert _checked(settings_section_screen("qh", prefs(), now=NOW)) == ["s:qh:off"]


def test_a_custom_window_and_a_timed_mute_check_nothing() -> None:
    custom = prefs(quiet_hours_start="23:30", quiet_hours_end="07:15")
    assert _checked(settings_section_screen("qh", custom, now=NOW)) == []
    assert "23:30" in settings_section_screen("qh", custom, now=NOW).text
    assert _checked(settings_section_screen("mu", BUSY, now=NOW)) == []


def test_throttle_and_timezone_show_their_commands_instead_of_buttons() -> None:
    text = settings_screen(BUSY, now=NOW).text
    assert "Europe/Berlin" in text
    assert "/timezone" in text
    assert "/throttle" in text
    assert "5 per hour" in text
    assert "unlimited" in settings_screen(prefs(), now=NOW).text


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("name", sorted(SCREENS))
def test_every_screen_fits_and_matches_its_snapshot_in_every_locale(
    name: str, locale: str, ui_locales: Path
) -> None:
    set_locale(locale)
    screen = SCREENS[name]()
    assert _is_valid_telegram_markup(screen.text)
    assert visible_length(screen.text) <= SAFE_LIMIT
    for row in screen.rows:
        assert len(row) in (1, 2), row
        limit = HALF_WIDTH if len(row) == 2 else ROW_WIDTH
        assert all(display_width(btn.label) <= limit for btn in row), row
    content = render_snapshot(screen, screen_name=f"panels.{name}", locale=locale, now=_NOW_TEXT)
    compare_or_update(_SNAPSHOT_DIR / f"{name}.{locale}.txt", content)


def test_no_orphan_panel_snapshots() -> None:
    expected = {f"{name}.{locale}.txt" for name in SCREENS for locale in LOCALES}
    on_disk = {path.name for path in _SNAPSHOT_DIR.glob("*.txt")}
    assert on_disk == expected


@pytest.mark.parametrize("locale", LOCALES)
def test_hostile_quiet_hours_values_stay_inert_in_every_locale(
    locale: str, ui_locales: Path
) -> None:
    set_locale(locale)
    view = prefs(quiet_hours_start=HOSTILE, quiet_hours_end=HOSTILE)
    for screen in (
        settings_screen(view, now=NOW),
        settings_section_screen("qh", view, now=NOW),
    ):
        assert _is_valid_telegram_markup(screen.text)
        assert visible_length(screen.text) <= SAFE_LIMIT
        assert "<b>&" not in screen.text
        assert "\n</b>" not in screen.text


@pytest.mark.parametrize(
    "changes",
    [
        {"mute": 1},
        {"mute_until": NOW.replace(tzinfo=None)},
        {"digest_interval_minutes": 0},
        {"throttle_per_hour": 0},
        {"quiet_hours_start": 5},
        {"timezone": "Not/AZone"},
        {"timezone": HOSTILE},
    ],
    ids=repr,
)
def test_the_view_refuses_malformed_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"."):
        prefs(**changes)


def test_the_view_is_frozen() -> None:
    view = prefs()
    assert isinstance(view, PrefsView)
    with pytest.raises(AttributeError):
        view.mute = True  # type: ignore[misc]


# --- Home ----------------------------------------------------------------------

HOME_LEGACY = ["menu_prezzi", "menu_notifiche", "menu_info", "menu_dati"]


@pytest.mark.parametrize(("is_admin", "extra"), [(False, []), (True, ["menu_admin"])])
def test_home_offers_the_areas_and_only_admins_get_the_admin_button(
    is_admin: bool, extra: list[str]
) -> None:
    screen = home_screen(HomeView(active=2, paused=0, is_admin=is_admin))
    assert _callbacks(screen) == [
        "l:a:1",
        "menu_prezzi",
        "menu_notifiche",
        "menu_dati",
        "menu_info",
        "s",
        *extra,
    ]
    for data in _callbacks(screen):
        assert data in [*HOME_LEGACY, "menu_admin"] or isinstance(decode(data), Action)


@pytest.mark.parametrize("locale", ["it", "en"])
@pytest.mark.parametrize(("is_admin", "tail"), [(False, []), (True, [["menu_admin"]])])
def test_home_is_a_tree_of_pairs_with_admin_alone(
    ui_locales: Path, locale: str, is_admin: bool, tail: list[list[str]]
) -> None:
    set_locale(locale)
    screen = home_screen(HomeView(active=2, paused=0, is_admin=is_admin))
    rows = [[btn.callback for btn in row] for row in screen.rows]
    assert rows == [
        ["l:a:1", "menu_prezzi"],
        ["menu_notifiche", "menu_dati"],
        ["menu_info", "s"],
        *tail,
    ]


def test_home_shows_the_counts(ui_locales: Path) -> None:
    set_locale("en")
    text = home_screen(HomeView(active=7, paused=2, is_admin=False)).text
    assert "7 active" in text
    assert "2 paused" in text


@pytest.mark.parametrize(
    "changes", [{"active": -1}, {"paused": -1}, {"active": True}, {"is_admin": 1}]
)
def test_the_home_view_refuses_malformed_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"."):
        HomeView(**{"active": 1, "paused": 1, "is_admin": False, **changes})  # type: ignore[arg-type]
