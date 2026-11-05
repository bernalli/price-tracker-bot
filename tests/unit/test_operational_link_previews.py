"""Operational notices are sent without link previews.

A quarantine or suspension notice names the store's domain, which Telegram turns
into a link and expands into a preview card of the very store that is blocking
us. Price alerts keep their previews.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from price_tracker.notifier.preferences import EffectivePrefs
from price_tracker.notifier.telegram import TelegramNotifier


def _prefs() -> EffectivePrefs:
    return EffectivePrefs(
        mute=False,
        mute_until=None,
        digest_mode=False,
        digest_interval_minutes=60,
        quiet_hours_start=None,
        quiet_hours_end=None,
        throttle_per_hour=None,
        timezone="UTC",
    )


def _preview_flags(bot: AsyncMock) -> list[object]:
    return [
        call.kwargs.get("disable_web_page_preview") for call in bot.send_message.await_args_list
    ]


@pytest.mark.parametrize("event", ["quarantine", "suspended", "warning"])
async def test_direct_operational_notice_has_no_preview(event: str) -> None:
    bot = AsyncMock()
    notifier = TelegramNotifier(bot)

    await notifier(1, "Paused: store.com", payload={"kind": "operational", "event": event})

    assert _preview_flags(bot) == [True]


@pytest.mark.parametrize("event", ["quarantine", "suspended", "warning"])
async def test_preference_routed_operational_notice_has_no_preview(event: str) -> None:
    bot = AsyncMock()
    prefs = AsyncMock()
    prefs.resolve_global = AsyncMock(return_value=_prefs())
    notifier = TelegramNotifier(bot, prefs=prefs)

    delivered = await notifier(
        1, "Paused: store.com", payload={"kind": "operational", "event": event}
    )

    assert delivered is True
    assert _preview_flags(bot) == [True]


async def test_long_operational_notice_has_no_preview_on_any_chunk() -> None:
    bot = AsyncMock()
    notifier = TelegramNotifier(bot)

    await notifier(1, "store.com\n" * 2000, payload={"kind": "operational"})

    flags = _preview_flags(bot)
    assert len(flags) > 1
    assert all(flag is True for flag in flags)


async def test_price_alert_keeps_its_preview() -> None:
    bot = AsyncMock()
    prefs = AsyncMock()
    prefs.resolve = AsyncMock(return_value=_prefs())
    notifier = TelegramNotifier(bot, prefs=prefs)

    await notifier(1, "Price drop", product_id=7, payload={"kind": "price_drop"})

    flags = _preview_flags(bot)
    assert len(flags) == 1
    assert flags[0] is not True
