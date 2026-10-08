"""The settings screens rendered by test_panels.py and test_snapshots.py."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from price_tracker.app.views import HomeView, PrefsView
from price_tracker.bot.ui.panels import (
    home_screen,
    product_prefs_screen,
    settings_screen,
    settings_section_screen,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from price_tracker.bot.ui.screens import Screen

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def prefs(**changes: object) -> PrefsView:
    """A view with every preference at its default, then ``changes`` applied."""
    base = PrefsView(
        mute=False,
        mute_until=None,
        digest_mode=False,
        digest_interval_minutes=60,
        quiet_hours_start=None,
        quiet_hours_end=None,
        throttle_per_hour=None,
        timezone="Europe/Rome",
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


BUSY = prefs(
    mute=True,
    mute_until=NOW + timedelta(hours=8),
    digest_mode=True,
    digest_interval_minutes=30,
    quiet_hours_start="22:00",
    quiet_hours_end="08:00",
    throttle_per_hour=5,
    timezone="Europe/Berlin",
)

SCREENS: dict[str, Callable[[], Screen]] = {
    "settings": lambda: settings_screen(BUSY, now=NOW),
    "settings_mute": lambda: settings_section_screen("mu", BUSY, now=NOW),
    "settings_digest": lambda: settings_section_screen("dg", BUSY, now=NOW),
    "settings_quiet": lambda: settings_section_screen("qh", BUSY, now=NOW),
    "settings_language": lambda: settings_section_screen("lang", BUSY, now=NOW, language="it"),
    "settings_timezone": lambda: settings_section_screen("tz", BUSY, now=NOW),
    "settings_throttle": lambda: settings_section_screen("th", BUSY, now=NOW),
    "product_prefs": lambda: product_prefs_screen("Kettle", 7, BUSY, now=NOW),
    "home_user": lambda: home_screen(HomeView(active=3, paused=1, is_admin=False)),
    "home_admin": lambda: home_screen(HomeView(active=3, paused=1, is_admin=True)),
}
