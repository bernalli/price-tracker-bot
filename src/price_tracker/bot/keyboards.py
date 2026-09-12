"""Reusable inline-keyboard builders for the bot UI.

Ported from monolithic bot.py [Task 17].
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from price_tracker.bot.messages import _


def build_threshold_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Build the standard threshold/notification choice keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _("\U0001f514 Every drop"),
                    callback_data=f"track_any_{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    _("\U0001f4c9 Threshold % or €"),
                    callback_data=f"track_threshold_{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    _("\U0001f4b0 Target price"),
                    callback_data=f"track_target_{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    _("\U0001f44d -10% is fine (default)"),
                    callback_data=f"track_default_{product_id}",
                ),
            ],
        ]
    )


def menu_back_button() -> list[InlineKeyboardButton]:
    """Single-row 'back to main menu' button."""
    return [InlineKeyboardButton(_("◀️ Menu"), callback_data="menu_main")]
