"""Reusable inline-keyboard builders for the bot UI.

Split out of the original monolithic bot.py module.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.messages import _


def build_threshold_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Build the standard threshold/notification choice keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\U0001f514 Ogni ribasso",
                    callback_data=encode(Action("product.threshold_any", (product_id,))),
                ),
            ],
            [
                InlineKeyboardButton(
                    "\U0001f4c9 Soglia % o €",
                    callback_data=encode(Action("product.threshold", (product_id,))),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🎯 Prezzo target",
                    callback_data=encode(Action("product.target", (product_id,))),
                ),
            ],
            [
                InlineKeyboardButton(
                    "📉 Va bene -10% (default)",
                    callback_data=encode(Action("product.threshold_default", (product_id,))),
                ),
            ],
        ]
    )


def menu_back_button() -> list[InlineKeyboardButton]:
    """Single-row 'back to main menu' button."""
    return [InlineKeyboardButton(_("⬅️ Menu"), callback_data=encode(Action("home")))]
