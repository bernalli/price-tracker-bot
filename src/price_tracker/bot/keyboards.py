"""Reusable inline-keyboard builders for the bot UI.

Split out of the original monolithic bot.py module.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def build_threshold_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Build the standard threshold/notification choice keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\U0001f514 Ogni ribasso",
                    callback_data=f"track_any_{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "\U0001f4c9 Soglia % o €",
                    callback_data=f"track_threshold_{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "\U0001f4b0 Prezzo target",
                    callback_data=f"track_target_{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "\U0001f44d Va bene -10% (default)",
                    callback_data=f"track_default_{product_id}",
                ),
            ],
        ]
    )


def menu_back_button() -> list[InlineKeyboardButton]:
    """Single-row 'back to main menu' button."""
    return [InlineKeyboardButton("◀️ Menu", callback_data="menu_main")]
