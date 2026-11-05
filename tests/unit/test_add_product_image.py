"""Adding a product persists and requests the image from the same reading."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from price_tracker.bot.handlers.product import _add_product
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scraper_base import ProductInfo
from tests.integration.test_guided_flow_wired import OWNER, PRIVATE, _wired
from tests.integration.test_scheduler import _StubScraper
from tests.support.fake_telegram import message_update
from tests.support.two_messages import wire_two_messages


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("/photo.jpg", "https://shop.example/photo.jpg"),
        ("https://shop.example/img.jpg#part", "https://shop.example/img.jpg"),
        ("https://[bad", None),
        (None, None),
    ],
)
@pytest.mark.parametrize("enabled", [True, False])
async def test_the_added_price_and_image_share_one_reading(
    tmp_path, monkeypatch, image, expected, enabled
):
    async with _wired() as w:
        registry = ScraperRegistry()
        registry.register(
            _StubScraper(
                ProductInfo(name="New item", price=Decimal(12), currency="EUR", image_url=image)
            )
        )
        w.app.bot_data["scraper"] = registry
        calls = []
        if enabled:
            mode = wire_two_messages(w, tmp_path, clock=lambda: 0)

            def requested(url):
                assert not w.conn.in_transaction
                calls.append(url)
                return False

            monkeypatch.setattr(mode.thumbnails, "request", requested)
        else:
            w.app.bot_data["http_client"] = object()
        update = message_update(w.app.bot, PRIVATE, OWNER, "https://shop.example/new-item")
        context: Any = SimpleNamespace(bot_data=w.app.bot_data)
        await _add_product(update, context, "https://shop.example/new-item")
        saved = await w.repo.get_product_by_url_for_user("https://shop.example/new-item", OWNER)
        assert saved is not None
        assert saved.image_url == expected
        assert saved.current_price == Decimal(12)
        assert calls == ([expected] if enabled else [])
        if enabled:
            await w.app.bot_data["http_client"].aclose()
