"""Verifies price_tracker.bot.ui.screens: Button and Screen."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from price_tracker.bot.ui.screens import Button, Screen


def test_button_is_frozen() -> None:
    btn = Button("x", callback="a")
    with pytest.raises(FrozenInstanceError):
        btn.label = "y"  # type: ignore[misc]


def test_screen_is_frozen() -> None:
    screen = Screen("t")
    with pytest.raises(FrozenInstanceError):
        screen.text = "u"  # type: ignore[misc]


def test_button_rejects_neither_callback_nor_url() -> None:
    with pytest.raises(ValueError, match=r"."):
        Button("x")


def test_button_rejects_both_callback_and_url() -> None:
    with pytest.raises(ValueError, match=r"."):
        Button("x", callback="a", url="u")


def test_button_rejects_empty_label() -> None:
    with pytest.raises(ValueError, match=r"."):
        Button("", callback="a")


def test_screen_defaults() -> None:
    screen = Screen("t")
    assert screen.rows == ()
    assert screen.disable_link_preview is True
