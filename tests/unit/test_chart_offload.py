"""Chart rendering must run off the event loop (#13).

_generate_chart did all the matplotlib work synchronously in the async handler,
blocking the whole bot for the render duration. The heavy work now runs in a
worker thread via asyncio.to_thread.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock

from price_tracker.bot.handlers import history


async def test_chart_renders_off_the_event_loop(monkeypatch) -> None:
    main_thread = threading.get_ident()
    captured: dict[str, int] = {}
    real_render = history._render_chart

    def spy(*args: object, **kwargs: object):  # noqa: ANN202
        captured["thread"] = threading.get_ident()
        return real_render(*args, **kwargs)

    monkeypatch.setattr(history, "_render_chart", spy)

    db = AsyncMock()
    db.get_price_history = AsyncMock(
        return_value=[
            {"checked_at": "2026-06-01T10:00:00", "price": "100"},
            {"checked_at": "2026-06-02T10:00:00", "price": "90"},
        ]
    )

    buf = await history._generate_chart(db, 1, {"name": "Widget"})

    assert buf is not None
    assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"  # valid PNG
    assert captured["thread"] != main_thread  # rendered in a worker thread
