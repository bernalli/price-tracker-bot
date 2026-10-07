"""The paginated product list: pagination, filters, keyboard and rendering in every locale."""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from price_tracker.app.views import PAGE_SIZE, ListPage
from price_tracker.bot.callbacks import ID_MAX, Action, decode
from price_tracker.bot.handlers._cards import list_view
from price_tracker.bot.messages import set_locale
from price_tracker.bot.ui.cards import list_page
from price_tracker.bot.ui.width import display_width
from price_tracker.core.textlimits import SAFE_LIMIT, _is_valid_telegram_markup, visible_length
from tests.support.list_variants import HOSTILE, SCREENS, record, view
from tests.support.ui_snapshot import compare_or_update, render_snapshot

if TYPE_CHECKING:
    from price_tracker.app.views import ListFilter
    from price_tracker.bot.ui.screens import Screen

_SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "list"
_NOW_TEXT = "2026-03-01T12:00:00Z"
HALF_WIDTH = 17
ROW_WIDTH = 34
LOCALES = ("en", "it", "zh_Hans", "fr", "es", "de", "uk", "pt_BR", "ja")
LEGACY_CALLBACKS = {"delete_all"}


def build(count: int, list_filter: ListFilter, page: int) -> ListPage:
    """``count`` products that match ``list_filter``."""
    fields = {"a": {}, "p": {"is_active": 0}, "e": {"consecutive_errors": 1}}[list_filter]
    rows = [record(i, **fields) for i in range(1, count + 1)]
    return list_view(rows, list_filter, page, default_interval_minutes=60)


def callbacks(screen: Screen) -> list[str]:
    return [btn.callback for row in screen.rows for btn in row if btn.callback is not None]


def ids(page: ListPage) -> list[int]:
    return [item.id for item in page.items]


# --- pagination ---------------------------------------------------------------


@pytest.mark.parametrize("count", range(24))
def test_pagination_slices_and_clamps(count: int) -> None:
    pages = max(1, math.ceil(count / PAGE_SIZE))
    for asked in (1, pages, pages + 1, ID_MAX):
        page = build(count, "a", asked)
        shown = min(asked, pages)
        expected = list(range(1, count + 1))[(shown - 1) * PAGE_SIZE : shown * PAGE_SIZE]
        assert (page.pages, page.page, page.total) == (pages, shown, count)
        assert ids(page) == expected


def test_page_size_is_five() -> None:
    assert PAGE_SIZE == 5


# --- filters ------------------------------------------------------------------

ROWS = {
    1: record(1),
    2: record(2, is_active=0),
    3: record(3, is_active=0, suspension_kind="automatic"),
    4: record(4, consecutive_errors=1),
    5: record(5, is_active=0, consecutive_errors=1),
    6: record(6, consecutive_errors=0),
}


@pytest.mark.parametrize(
    ("list_filter", "expected"),
    [("a", [1, 4, 6]), ("p", [2, 3, 5]), ("e", [4, 5])],
)
def test_each_filter_keeps_exactly_its_products(
    list_filter: ListFilter, expected: list[int]
) -> None:
    page = list_view(list(ROWS.values()), list_filter, 1, default_interval_minutes=60)
    assert ids(page) == expected
    assert page.total == len(expected)


def test_a_filter_without_products_is_an_empty_single_page() -> None:
    page = list_view([record(1)], "p", 3, default_interval_minutes=60)
    assert (page.items, page.total, page.page, page.pages) == ((), 0, 1, 1)


def test_the_view_refuses_malformed_pages() -> None:
    good: dict[str, Any] = {"filter": "a", "page": 1, "pages": 1, "total": 1, "items": (view(1),)}
    for change in (
        {"filter": "x"},
        {"page": 0},
        {"page": 2},
        {"pages": 0},
        {"total": -1},
        {"items": (view(1),) * (PAGE_SIZE + 1)},
    ):
        with pytest.raises(ValueError, match=r"."):
            ListPage(**{**good, **change})


# --- keyboard -----------------------------------------------------------------


def test_every_callback_decodes_or_is_a_legacy_one(ui_locales: Path) -> None:
    set_locale("en")
    for count, asked in ((0, 1), (1, 1), (7, 1), (7, 2), (12, 2), (12, 3)):
        for list_filter in ("a", "p", "e"):
            screen = list_page(build(count, list_filter, asked))
            for data in callbacks(screen):
                assert isinstance(decode(data), Action) or data in LEGACY_CALLBACKS, data


def test_products_open_the_card_on_this_page_and_filter(ui_locales: Path) -> None:
    set_locale("en")
    screen = list_page(build(12, "e", 2))
    opens = [decode(d) for d in callbacks(screen) if d.startswith("l:e:2:")]
    assert opens == [Action("list.open", ("e", 2, i)) for i in range(6, 11)]


@pytest.mark.parametrize("list_filter", ["a", "p", "e"])
def test_exactly_one_filter_carries_the_check_mark(
    list_filter: ListFilter, ui_locales: Path
) -> None:
    set_locale("en")
    screen = list_page(build(3, list_filter, 1))
    checked = [btn for row in screen.rows for btn in row if btn.label.endswith("✓")]
    assert [btn.callback for btn in checked] == [f"l:{list_filter}:1"]
    filters = {f"l:{f}:1" for f in "ape"}
    assert filters <= set(callbacks(screen))


def test_navigation_follows_the_page(ui_locales: Path) -> None:
    set_locale("en")

    def nav(count: int, asked: int) -> list[str]:
        screen = list_page(build(count, "a", asked))
        row = next((r for r in screen.rows if any(b.callback == "noop" for b in r)), ())
        return [b.callback or "" for b in row]

    assert nav(5, 1) == []
    assert nav(0, 1) == []
    assert nav(12, 1) == ["noop", "l:a:2"]
    assert nav(12, 2) == ["l:a:1", "noop", "l:a:3"]
    assert nav(12, 3) == ["l:a:2", "noop"]
    screen = list_page(build(12, "a", 2))
    row = next(r for r in screen.rows if any(b.callback == "noop" for b in r))
    assert row[1].label == "2/3"


@pytest.mark.parametrize(
    ("count", "list_filter", "expected"),
    [(2, "a", True), (1, "a", False), (0, "a", False), (3, "p", False), (3, "e", False)],
)
def test_delete_all_only_for_several_active_products(
    count: int, list_filter: ListFilter, expected: bool, ui_locales: Path
) -> None:
    set_locale("en")
    screen = list_page(build(count, list_filter, 1))
    assert ("delete_all" in callbacks(screen)) is expected
    assert "h" in callbacks(screen)


# --- text ---------------------------------------------------------------------


def test_the_header_and_the_rows(ui_locales: Path) -> None:
    set_locale("en")
    page = ListPage(
        "a",
        1,
        1,
        3,
        (
            view(1, "Kettle"),
            view(2, "Fan", status="paused"),
            view(3, "Lamp", errors=1, current=None),
        ),
    )
    lines = list_page(page).text.split("\n")
    assert lines[0] == "📦 <b>Your products</b> · active (3) · page 1/1"
    assert lines[2:] == [
        "<b>#1</b> Kettle · €19.99",
        "⏸ <b>#2</b> Fan · €19.99",
        "⚠️ <b>#3</b> Lamp · —",
    ]


def test_an_empty_filter_says_so_and_keeps_filters_and_home(ui_locales: Path) -> None:
    set_locale("en")
    screen = list_page(build(0, "e", 1))
    assert screen.text.endswith("Nothing here.")
    assert "noop" not in callbacks(screen)
    assert {"l:a:1", "l:p:1", "l:e:1", "h"} <= set(callbacks(screen))


def test_a_long_name_is_cut_to_forty_cells(ui_locales: Path) -> None:
    set_locale("en")
    row = list_page(ListPage("a", 1, 1, 1, (view(1, "N" * 100),))).text.split("\n")[2]
    name = row.split("</b> ", 1)[1].split(" · ", 1)[0]
    assert display_width(name) <= 40
    assert name.endswith("…")


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("name", sorted(SCREENS))
def test_every_screen_fits_and_matches_its_snapshot_in_every_locale(
    name: str, locale: str, ui_locales: Path
) -> None:
    set_locale(locale)
    screen = SCREENS[name]()
    assert _is_valid_telegram_markup(screen.text)
    assert visible_length(screen.text) <= SAFE_LIMIT
    assert "<b>&" not in screen.text
    assert "\n</b>" not in screen.text
    for row in screen.rows:
        if any(btn.callback and btn.callback.count(":") == 3 for btn in row):
            limit = HALF_WIDTH if len(row) == 2 else ROW_WIDTH
            assert len(row) <= 2
            assert all(display_width(btn.label) <= limit for btn in row), row
    content = render_snapshot(screen, screen_name=f"list.{name}", locale=locale, now=_NOW_TEXT)
    compare_or_update(_SNAPSHOT_DIR / f"{name}.{locale}.txt", content)


def test_no_orphan_list_snapshots() -> None:
    expected = {f"{name}.{locale}.txt" for name in SCREENS for locale in LOCALES}
    assert {path.name for path in _SNAPSHOT_DIR.glob("*.txt")} == expected


@pytest.mark.parametrize("locale", LOCALES)
def test_five_hostile_rows_stay_inside_the_limit(locale: str, ui_locales: Path) -> None:
    set_locale(locale)
    items = tuple(view(i, HOSTILE) for i in range(1, 6))
    screen = list_page(ListPage("a", 1, 1, 5, items))
    assert visible_length(screen.text) < 2000
    assert _is_valid_telegram_markup(screen.text)
