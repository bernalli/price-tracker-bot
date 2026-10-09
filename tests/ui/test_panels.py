"""The settings panels: pure screens, in every supported locale."""

from __future__ import annotations

import html
import re
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from price_tracker.app.views import HomeView, PrefsView
from price_tracker.bot.callbacks import Action, InvalidCallback, decode, encode
from price_tracker.bot.messages import set_locale
from price_tracker.bot.ui.panels import (
    add_screen,
    home_screen,
    product_prefs_screen,
    settings_screen,
    settings_section_screen,
)
from price_tracker.bot.ui.width import display_width
from price_tracker.core.textlimits import (
    NAME_BUDGET,
    SAFE_LIMIT,
    _is_valid_telegram_markup,
    visible_length,
)
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


def test_main_panel_offers_the_six_sections_and_home() -> None:
    assert _actions(settings_screen(BUSY, now=NOW)) == [
        Action("settings.section", ("mu",)),
        Action("settings.section", ("dg",)),
        Action("settings.section", ("qh",)),
        Action("settings.section", ("th",)),
        Action("settings.section", ("tz",)),
        Action("settings.section", ("lang",)),
        Action("home"),
    ]


@pytest.mark.parametrize(
    ("section", "values", "action", "extra"),
    [
        ("mu", ["1", "8", "24", "0", "off"], "settings.mute", [Action("settings.ask", ("mu",))]),
        (
            "dg",
            ["on", "off"],
            "settings.digest",
            [Action("settings.ask", ("dg",)), Action("settings.digest_now")],
        ),
        ("qh", ["2208", "off"], "settings.quiet", [Action("settings.ask", ("qh",))]),
        ("lang", ["auto", "en", "it"], "settings.language", []),
        ("tz", [], "", [Action("settings.ask", ("tz",))]),
        ("th", [], "", [Action("settings.ask", ("th",))]),
    ],
)
def test_section_offers_its_presets_a_way_back_and_home(
    section: str, values: list[str], action: str, extra: list[Action]
) -> None:
    actions = _actions(settings_section_screen(section, BUSY, now=NOW))
    presets = [Action(action, (value,)) for value in values]
    assert actions == [*presets, *extra, Action("settings"), Action("home")]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("section", ["mu", "dg", "qh", "lang", "tz", "th"])
def test_every_section_ends_with_back_and_home(section: str, locale: str, ui_locales: Path) -> None:
    set_locale(locale)
    rows = settings_section_screen(section, BUSY, now=NOW).rows
    tail = [button.callback for row in rows for button in row][-2:]
    assert tail == ["s", "h"]


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


def test_throttle_and_timezone_show_their_values() -> None:
    text = settings_screen(BUSY, now=NOW).text
    assert "Europe/Berlin" in text
    assert "5 per hour" in text
    assert "unlimited" in settings_screen(prefs(), now=NOW).text
    assert "Europe/Berlin" in settings_section_screen("tz", BUSY, now=NOW).text
    assert "5 per hour" in settings_section_screen("th", BUSY, now=NOW).text
    assert "unlimited" in settings_section_screen("th", prefs(), now=NOW).text


_TYPED_COMMANDS = ("/throttle", "/timezone", "/digest_mode", "/quiet_hours", "/mute")


@pytest.mark.parametrize("locale", LOCALES)
def test_no_settings_screen_tells_the_user_to_type_a_command(locale: str, ui_locales: Path) -> None:
    set_locale(locale)
    screens = [settings_screen(BUSY, now=NOW)] + [
        settings_section_screen(section, BUSY, now=NOW)
        for section in ("mu", "dg", "qh", "tz", "th", "lang")
    ]
    screens.append(product_prefs_screen("Kettle", 7, BUSY, now=NOW))
    for screen in screens:
        assert not any(command in screen.text for command in _TYPED_COMMANDS), screen.text


# --- the notifications of one product -------------------------------------------


def test_product_notifications_offer_mute_presets_settings_product_and_home() -> None:
    actions = _actions(product_prefs_screen("Kettle", 7, BUSY, now=NOW))
    assert actions == [
        *(Action("product.mute", (7, value)) for value in ("1", "8", "24", "0", "off")),
        Action("product.mute_ask", (7,)),
        Action("settings"),
        Action("product.card", (7,)),
        Action("home"),
    ]


def test_product_notifications_show_the_five_effective_values(ui_locales: Path) -> None:
    set_locale("en")
    text = product_prefs_screen("Kettle", 7, BUSY, now=NOW).text
    for expected in ("Kettle", "until", "every 30", "22:00", "5 per hour", "Europe/Berlin"):
        assert expected in text, expected


def test_product_notifications_mark_the_current_mute() -> None:
    assert _checked(product_prefs_screen("K", 7, prefs(), now=NOW)) == ["p:7:mu:off"]
    assert _checked(product_prefs_screen("K", 7, prefs(mute=True), now=NOW)) == ["p:7:mu:0"]
    ended = prefs(mute=True, mute_until=NOW - timedelta(seconds=1))
    assert product_prefs_screen("K", 7, ended, now=NOW) == product_prefs_screen(
        "K", 7, prefs(), now=NOW
    )
    assert "unlimited" in product_prefs_screen("K", 7, prefs(), now=NOW).text


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("name", [HOSTILE, "<b>&" * 200, "x" * 500, "電気ケトル" * 100])
def test_product_notifications_name_stays_inert_and_short(
    locale: str, name: str, ui_locales: Path
) -> None:
    set_locale(locale)
    screen = product_prefs_screen(name, 7, BUSY, now=NOW)
    assert _is_valid_telegram_markup(screen.text)
    assert visible_length(screen.text) <= SAFE_LIMIT
    assert "<b>&" not in screen.text
    if locale == "en":
        name_line = html.unescape(re.sub(r"<[^>]+>", "", screen.text.split("\n")[1]))
        assert display_width(name_line) <= NAME_BUDGET + 4, name_line


@pytest.mark.parametrize("product_id", [0, -1, True, "7"])
def test_product_notifications_refuse_a_bad_id(product_id: object) -> None:
    with pytest.raises(ValueError, match="product_id"):
        product_prefs_screen("K", product_id, BUSY, now=NOW)  # type: ignore[arg-type]


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


@pytest.mark.parametrize(("is_admin", "extra"), [(False, []), (True, [encode(Action("admin"))])])
def test_home_offers_the_areas_and_only_admins_get_the_admin_button(
    is_admin: bool, extra: list[str]
) -> None:
    screen = home_screen(HomeView(active=2, paused=0, is_admin=is_admin))
    assert _callbacks(screen) == [
        "l:a:1",
        encode(Action("prices")),
        encode(Action("notifications")),
        encode(Action("data")),
        encode(Action("stats")),
        "s",
        *extra,
    ]
    for data in _callbacks(screen):
        assert isinstance(decode(data), Action)


@pytest.mark.parametrize("locale", ["it", "en"])
@pytest.mark.parametrize(("is_admin", "tail"), [(False, []), (True, [[encode(Action("admin"))]])])
def test_home_is_a_tree_of_pairs_with_admin_alone(
    ui_locales: Path, locale: str, is_admin: bool, tail: list[list[str]]
) -> None:
    set_locale(locale)
    screen = home_screen(HomeView(active=2, paused=0, is_admin=is_admin))
    rows = [[btn.callback for btn in row] for row in screen.rows]
    assert rows == [
        ["l:a:1", encode(Action("prices"))],
        [encode(Action("notifications")), encode(Action("data"))],
        [encode(Action("stats")), "s"],
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


# --- Add -------------------------------------------------------------------------


@pytest.mark.parametrize("locale", LOCALES)
def test_add_asks_for_a_link_and_leads_back_to_the_list_and_home(
    locale: str, ui_locales: Path
) -> None:
    set_locale(locale)
    screen = add_screen()
    assert _callbacks(screen) == ["l:a:1", "h"]
    assert _is_valid_telegram_markup(screen.text)
    assert "/add" not in screen.text


def test_add_says_to_paste_a_link(ui_locales: Path) -> None:
    set_locale("en")
    assert "Paste" in add_screen().text
    set_locale("it")
    assert "Incolla" in add_screen().text
    set_locale("en")
