"""The product name in the chart title is literal text, not mathtext.

matplotlib parses text between two `$` signs as mathtext, so a product named
`Cable $\\zz$` made `_render_chart` raise ValueError and `/storia` ended in the
error handler instead of sending the chart.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from price_tracker.bot.handlers.history import _render_chart

DATES = [datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 2, tzinfo=UTC)]
PRICES = [100.0, 90.0]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize(
    "name",
    [
        "Cable $\\zz$",
        "Widget $\\frac{1}$",
        "Pack of 2 $ and $ 3",
        "Adapter $",
    ],
)
def test_render_chart_treats_dollar_signs_in_the_name_as_text(name: str) -> None:
    buf = _render_chart(DATES, PRICES, None, name)

    assert buf.getvalue().startswith(PNG_SIGNATURE)
