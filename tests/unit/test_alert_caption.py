"""A photo caption is cut at Telegram's 1024 limit without breaking the markup."""

from __future__ import annotations

import io
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from price_tracker.bot.handlers import history
from price_tracker.bot.handlers.monitoring import _send_alert
from price_tracker.core.alert import PriceAlert, format_alert
from price_tracker.core.textlimits import _is_valid_telegram_markup, visible_length

CAPTION_LIMIT = 1024


def _alert(name: str) -> PriceAlert:
    return PriceAlert(
        product_id=1,
        product_name=name,
        url="https://shop.example/p/1",
        old_price=Decimal("100"),
        new_price=Decimal("80"),
        currency="EUR",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )


async def _caption(alert: PriceAlert) -> str:
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    db = AsyncMock()
    db.get_product.return_value = {"id": 1}
    with patch.object(history, "_generate_chart", AsyncMock(return_value=io.BytesIO(b"png"))):
        await _send_alert(bot, alert, db, chat_id=10)
    return str(bot.send_photo.await_args.kwargs["caption"])


@pytest.mark.asyncio
async def test_an_oversized_caption_is_plain_escaped_text_within_the_limit() -> None:
    alert = _alert("K" * 3000)
    assert visible_length(format_alert(alert)) > CAPTION_LIMIT
    caption = await _caption(alert)
    assert visible_length(caption) <= CAPTION_LIMIT
    assert caption.endswith("…")
    assert _is_valid_telegram_markup(caption)


@pytest.mark.asyncio
async def test_a_caption_that_fits_is_sent_unchanged() -> None:
    alert = _alert("Kettle")
    assert await _caption(alert) == format_alert(alert)
