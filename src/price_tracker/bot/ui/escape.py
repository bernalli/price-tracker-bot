"""The single HTML escaper every renderer in this package uses."""

from __future__ import annotations

import html
import unicodedata
from typing import Final

# Superset of `price_tracker.core.alert._escape_html`: that helper only
# flattens \r and \n (both Cc); this flattens every Cc (control character,
# newlines included) plus Zl/Zp (line/paragraph separator), so a user- or
# site-controlled value can never leave the renderer row it belongs to.
_FLATTEN_CATEGORIES: Final = frozenset({"Cc", "Zl", "Zp"})


def escape_html(text: str) -> str:
    """Flatten line-breaking control characters to a space, then escape HTML.

    Does not truncate and does not touch ``Cf`` (format characters, including
    bidi isolates and ZWJ): callers that need those stripped use
    ``price_tracker.bot.ui.width.sanitize_label``.
    """
    if not isinstance(text, str):
        raise TypeError(f"text: must be a str, got {text!r}")
    flattened = "".join(
        " " if unicodedata.category(char) in _FLATTEN_CATEGORIES else char for char in text
    )
    return html.escape(flattened, quote=True)
