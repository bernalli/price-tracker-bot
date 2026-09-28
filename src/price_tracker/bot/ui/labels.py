"""Buttons and the budgeted row layout every screen keyboard uses."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from price_tracker.bot.ui.screens import Button
from price_tracker.bot.ui.width import display_width, sanitize_label, truncate_to_width

if TYPE_CHECKING:
    from collections.abc import Sequence

ROW_WIDTH: Final = 34
HALF_WIDTH: Final = 17

# ASCII, no whitespace, no control characters. The callback-data wire grammar
# itself (namespace, token count, argument shapes) belongs to the callback
# registry, not to this renderer-side check.
_CALLBACK_RE: Final = re.compile(r"[\x21-\x7e]+")


def button(text: str, *, callback: str | None = None, url: str | None = None) -> Button:
    """Build a ``Button`` with a sanitized, not-yet-truncated label.

    ``layout_rows`` does the truncation, because it is the one that knows the
    row budget the label must fit.
    """
    if (callback is None) == (url is None):
        raise ValueError("exactly one of callback or url is required")
    if callback is not None:
        if not isinstance(callback, str) or not callback.isascii():
            raise ValueError(f"callback: must be an ASCII str, got {callback!r}")
        byte_length = len(callback.encode("ascii"))
        if not 1 <= byte_length <= 64:
            raise ValueError(f"callback: must be 1..64 bytes, got {byte_length}")
        if _CALLBACK_RE.fullmatch(callback) is None:
            raise ValueError(
                f"callback: must have no spaces or control characters, got {callback!r}"
            )
    if url is not None and not url:
        raise ValueError("url must not be empty")
    label = sanitize_label(text)
    if not label:
        raise ValueError("text sanitizes to an empty label")
    return Button(label, callback, url)


def layout_rows(*groups: Sequence[Button]) -> tuple[tuple[Button, ...], ...]:
    """Pack each group into rows of one or two buttons, budget-truncated.

    Two buttons share a row only if both labels are within ``HALF_WIDTH``; a
    wider button takes a full row, truncated to ``ROW_WIDTH``. Groups are
    packed independently, in the order given; a button never crosses a group
    boundary into a neighboring row.
    """
    rows: list[tuple[Button, ...]] = []
    for group in groups:
        buttons = list(group)
        index = 0
        while index < len(buttons):
            current = buttons[index]
            has_pair = (
                index + 1 < len(buttons)
                and display_width(current.label) <= HALF_WIDTH
                and display_width(buttons[index + 1].label) <= HALF_WIDTH
            )
            if has_pair:
                rows.append((current, buttons[index + 1]))
                index += 2
                continue
            if display_width(current.label) > ROW_WIDTH:
                truncated_label = truncate_to_width(current.label, ROW_WIDTH)
                current = Button(truncated_label, current.callback, current.url)
            rows.append((current,))
            index += 1
    return tuple(rows)
