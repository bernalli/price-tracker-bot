"""Variant transitions and failed writes never split a price from its image."""

from decimal import Decimal

import aiosqlite
import httpx
import pytest

from price_tracker.core.domain_spacing import DomainSpacing
from price_tracker.core.scraper_base import ProductInfo
from price_tracker.media.cache import ThumbnailCache
from price_tracker.media.fetch import ImageFetchError
from price_tracker.media.thumbnails import ThumbnailService
from tests.integration.test_guided_flow_wired import _wired
from tests.integration.test_scheduler_image_capture import NEW, OLD, check


async def test_a_new_variant_without_a_photo_clears_the_previous_variant_image():
    async with _wired() as w:
        await w.repo.update_price_and_image(w.product, Decimal(100), OLD)
        await check(w, ProductInfo(price=Decimal(90), currency="EUR", image_url=None))
        saved = await w.repo.get_product(w.product)
        assert saved is not None
        assert (saved.current_price, saved.image_url) == (Decimal(90), None)


async def test_a_rejected_variant_read_keeps_the_previous_price_and_image():
    async with _wired() as w:
        await w.repo.update_price_and_image(w.product, Decimal(100), OLD)
        await check(w, ProductInfo(price=Decimal(0), currency="EUR", image_url=NEW))
        saved = await w.repo.get_product(w.product)
        assert saved is not None
        assert (saved.current_price, saved.image_url) == (Decimal(100), OLD)


async def test_a_failed_download_does_not_erase_the_committed_url(tmp_path, monkeypatch):
    async def fail(*args):
        raise ImageFetchError("download failed")

    monkeypatch.setattr("price_tracker.media.thumbnails.download_image", fail)
    async with _wired() as w, httpx.AsyncClient() as client:
        service = ThumbnailService(
            ThumbnailCache(tmp_path / "thumbs"),
            client=client,
            job_pacing=DomainSpacing(min_gap=0),
            referenced_urls=w.repo.list_image_urls,
        )
        await check(
            w, ProductInfo(price=Decimal(90), currency="EUR", image_url=NEW), requester=service
        )
        await service.process_one(NEW)
        assert not service.request(NEW)
        assert service.cached_bytes(NEW) is None
        saved = await w.repo.get_product(w.product)
        assert saved is not None
        assert saved.image_url == NEW


async def test_an_aborted_update_preserves_both_columns():
    async with _wired() as w:
        await w.repo.update_price_and_image(w.product, Decimal(100), OLD)
        await w.conn.execute(
            "CREATE TRIGGER abort_image BEFORE UPDATE OF image_url ON products "
            "BEGIN SELECT RAISE(ABORT, 'write failed'); END"
        )
        await w.conn.commit()
        with pytest.raises(aiosqlite.IntegrityError, match="write failed"):
            await w.repo.update_price_and_image(w.product, Decimal(90), NEW)
        await w.conn.rollback()
        saved = await w.repo.get_product(w.product)
        assert saved is not None
        assert (saved.current_price, saved.image_url) == (Decimal(100), OLD)
