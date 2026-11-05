"""Only accepted price readings persist and enqueue their own verified image."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.image_url import normalize_image_url
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import ProductInfo
from tests.integration.test_guided_flow_wired import OWNER, _wired
from tests.integration.test_scheduler import _StubScraper

OLD = "https://shop.example/old.jpg"
NEW = "https://shop.example/new.jpg"


class Requests:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.calls: list[str | None] = []
        self.queued: list[str] = []

    def request(self, url: str | None) -> bool:
        assert not self.connection.in_transaction
        self.calls.append(url)
        if url is not None:
            self.queued.append(url)
        return url is not None


async def check(w: Any, info: ProductInfo, *, requester: Any = True) -> Any:
    registry = ScraperRegistry()
    registry.register(_StubScraper(info))
    requests = Requests(w.conn) if requester is True else (requester if requester else None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("unexpected network"))
    ) as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=w.repo,
                registry=registry,
                client=client,
                notifier=AsyncMock(),
                delay_between_products=0,
                thumbnails=requests,
            )
        )
        writes: list[Any] = []
        original = w.repo.update_price_and_image

        async def write(*args: Any) -> None:
            writes.append(args)
            await original(*args)

        with patch.object(w.repo, "update_price_and_image", write):
            await scheduler.run_check_for_user(user_id=OWNER)
    return SimpleNamespace(requests=requests, scheduler=scheduler, writes=writes)


@pytest.mark.parametrize(
    "branch",
    [
        "accepted",
        "same",
        "sold_out",
        "missing_price",
        "currency",
        "condition",
        "rejected",
        "confirmation",
    ],
)
async def test_only_the_acceptance_branch_replaces_and_queues_the_image(branch):
    async with _wired() as w:
        await w.repo.update_price_and_image(w.product, Decimal(100), OLD)
        for _ in range(5):
            await w.repo.add_price_history(w.product, Decimal(100))
        info = ProductInfo(price=Decimal(90), currency="EUR", image_url=NEW)
        if branch == "same":
            info.image_url = OLD
        elif branch == "sold_out":
            info.available = False
        elif branch == "missing_price":
            info.price = None
        elif branch == "currency":
            info.currency = "USD"
        elif branch == "condition":
            await w.conn.execute(
                "UPDATE products SET preferred_condition='new' WHERE id=?", (w.product,)
            )
            await w.conn.commit()
            info.condition = "used"
        elif branch == "rejected":
            info.price = Decimal("0")
        elif branch == "confirmation":
            info.price = Decimal("40")
        result = await check(w, info)
        saved = await w.repo.get_product(w.product)
        assert saved is not None
        accepted = branch in ("accepted", "same")
        assert saved.image_url == (info.image_url if accepted else OLD)
        assert saved.current_price == (info.price if accepted else Decimal(100))
        assert result.requests.calls == ([info.image_url] if accepted else [])
        assert len(result.writes) == int(accepted)


@pytest.mark.parametrize(
    "image",
    [
        None,
        "http://shop.example/a.jpg",
        "https://[bad",
        "https://shop.example:bad/a",
        "https://shop.example:99999/a",
        "https://shop.example/\ud800",
        "/relative.jpg",
    ],
)
async def test_bad_or_missing_images_are_cleared_with_an_accepted_price(image):
    async with _wired() as w:
        await w.repo.update_price_and_image(w.product, Decimal(100), OLD)
        result = await check(w, ProductInfo(price=Decimal(90), currency="EUR", image_url=image))
        saved = await w.repo.get_product(w.product)
        assert saved is not None
        assert saved.current_price == Decimal(90)
        assert saved.image_url == normalize_image_url(image, base_url=saved.url)
        assert result.requests.queued == ([saved.image_url] if saved.image_url else [])


async def test_a_scheduler_without_thumbnail_services_still_persists_the_image():
    async with _wired() as w:
        await check(
            w, ProductInfo(price=Decimal(90), currency="EUR", image_url=NEW), requester=False
        )
        saved = await w.repo.get_product(w.product)
        assert saved is not None
        assert saved.image_url == NEW


@given(
    image=st.one_of(
        st.none(),
        st.text(max_size=300),
        st.sampled_from((NEW, "/image.jpg", "https://[bad", "https://shop.example:bad/p")),
    )
)
@settings(max_examples=80, deadline=None)
async def test_arbitrary_image_data_never_breaks_price_persistence(image):
    async with _wired() as w:
        await check(w, ProductInfo(price=Decimal(90), currency="EUR", image_url=image))
        saved = await w.repo.get_product(w.product)
        assert saved is not None
        assert saved.image_url == normalize_image_url(image, base_url=saved.url)
        assert saved.current_price == Decimal(90)
