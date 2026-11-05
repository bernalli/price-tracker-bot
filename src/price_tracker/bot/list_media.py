"""Present a cached product grid beside an independently navigable text message."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from telegram import InputMediaPhoto, LinkPreviewOptions
from telegram.constants import ParseMode

from price_tracker.bot.handlers._cards import screen_markup
from price_tracker.bot.list_grid import MAX_TILES, GridRegistry, TileKey
from price_tracker.bot.messages import current_locale
from price_tracker.bot.telegram_outcome import OutcomeKind, attempt
from price_tracker.i18n.format import money
from price_tracker.media.grid import Tile, render_grid
from price_tracker.media.thumbnails import ThumbnailService

if TYPE_CHECKING:
    from collections.abc import Sequence

    from telegram import CallbackQuery, Message

    from price_tracker.app.views import ProductView
    from price_tracker.bot.list_grid import GridFingerprint
    from price_tracker.bot.telegram_outcome import Outcome
    from price_tracker.bot.ui.screens import Screen

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TwoMessageMode:
    """The services the two-message list needs; present only when both are wired."""

    thumbnails: ThumbnailService
    grids: GridRegistry


@dataclass(frozen=True, slots=True)
class GridImage:
    """A grid's fingerprint and its JPEG; ``jpeg`` is None when it matched ``unchanged_from``."""

    fingerprint: GridFingerprint
    jpeg: bytes | None


def two_message_mode(context: object) -> TwoMessageMode | None:
    bot_data = getattr(context, "bot_data", None)
    if not isinstance(bot_data, dict):
        return None
    thumbnails = bot_data.get("thumbnails")
    grids = bot_data.get("list_grids")
    if not isinstance(thumbnails, ThumbnailService) or not isinstance(grids, GridRegistry):
        return None
    return TwoMessageMode(thumbnails, grids)


def tile_price(view: ProductView) -> str:
    if view.current is None:
        return "—"
    try:
        return money(view.current, view.currency, locale=current_locale())
    except ValueError:
        return f"{view.current:.2f}"


async def grid_image(
    items: Sequence[ProductView],
    service: ThumbnailService,
    *,
    unchanged_from: GridFingerprint | None = None,
) -> GridImage | None:
    try:
        if len(items) > MAX_TILES:
            raise ValueError("too many grid tiles")
        keys = []
        tiles = []
        for number, view in enumerate(items, 1):
            price = tile_price(view)
            thumb = await asyncio.to_thread(service.cached_bytes, view.image_url)
            keys.append(TileKey(number, view.id, price, view.image_url, thumb is not None))
            tiles.append(Tile(number, price, thumb))
        fingerprint = tuple(keys)
        if fingerprint == unchanged_from:
            return GridImage(fingerprint, None)
        jpeg = await asyncio.to_thread(render_grid, tiles)
        return GridImage(fingerprint, jpeg)
    except Exception:  # noqa: BLE001 — the list must never break on a rendering failure
        logger.warning("product grid rendering failed", exc_info=True)
        return None


def _note(grids: GridRegistry, outcome: Outcome, operation: str) -> None:
    if outcome.kind is OutcomeKind.RETRY_AFTER:
        assert outcome.retry_after is not None
        grids.note_retry_after(outcome.retry_after)
        logger.info("list %s rate limited", operation)
    elif outcome.kind in (OutcomeKind.REJECTED, OutcomeKind.UNCERTAIN):
        logger.warning("list %s %s", operation, outcome.kind.value)


async def send_list(
    message: Message, mode: TwoMessageMode, screen: Screen, items: Sequence[ProductView]
) -> None:
    async with mode.grids.lock:
        if len(items) > MAX_TILES or not screen.rows:
            raise ValueError("list requires at most six items and a keyboard")
        photo = None
        image = None
        if items and not mode.grids.cooling_down():
            image = await grid_image(items, mode.thumbnails)
            if image is not None and image.jpeg is not None:
                photo = await attempt(
                    message.reply_photo(photo=image.jpeg, filename="products.jpg")
                )
                _note(mode.grids, photo, "send photo")
        text = await attempt(
            message.reply_text(
                screen.text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=screen_markup(screen),
            )
        )
        _note(mode.grids, text, "send text")
        if (
            photo is not None
            and photo.kind is OutcomeKind.APPLIED
            and photo.message is not None
            and text.kind is OutcomeKind.APPLIED
            and text.message is not None
            and image is not None
        ):
            mode.grids.register(
                (text.message.chat.id, text.message.message_id),
                photo.message.message_id,
                image.fingerprint,
            )


async def edit_list(
    query: CallbackQuery, mode: TwoMessageMode, screen: Screen, items: Sequence[ProductView]
) -> None:
    if query.message is None:
        logger.info("list callback has no message")
        return
    key = (query.message.chat.id, query.message.message_id)
    async with mode.grids.lock:
        text = await attempt(
            query.get_bot().edit_message_text(
                chat_id=key[0],
                message_id=key[1],
                text=screen.text,
                parse_mode=ParseMode.HTML,
                reply_markup=screen_markup(screen),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        )
        _note(mode.grids, text, "edit text")
        if text.kind is not OutcomeKind.APPLIED:
            return
        entry = mode.grids.get(key)
        if entry is None or mode.grids.cooling_down():
            return
        image = await grid_image(items, mode.thumbnails, unchanged_from=entry.fingerprint)
        if image is None or image.jpeg is None:
            return
        grid = await attempt(
            query.get_bot().edit_message_media(
                media=InputMediaPhoto(media=image.jpeg, filename="products.jpg"),
                chat_id=key[0],
                message_id=entry.grid_message_id,
            )
        )
        mode.grids.apply_grid_outcome(key, grid.kind, image.fingerprint)
        _note(mode.grids, grid, "edit photo")


async def edit_card(
    query: CallbackQuery, mode: TwoMessageMode, screen: Screen, image_url: str | None
) -> None:
    if query.message is None:
        logger.info("card callback has no message")
        return
    async with mode.grids.lock:
        preview = (
            LinkPreviewOptions(url=image_url, prefer_large_media=True, show_above_text=True)
            if image_url is not None and not mode.grids.preview_refused(image_url)
            else LinkPreviewOptions(is_disabled=True)
        )
        card = await attempt(
            query.get_bot().edit_message_text(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                text=screen.text,
                parse_mode=ParseMode.HTML,
                reply_markup=screen_markup(screen),
                link_preview_options=preview,
            )
        )
        _note(mode.grids, card, "edit card")
        if (
            preview.url is not None
            and card.kind is OutcomeKind.REJECTED
            and card.reason.startswith("BadRequest:")
            and "message to edit not found" not in card.reason.lower()
        ):
            fallback = await attempt(
                query.get_bot().edit_message_text(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    text=screen.text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=screen_markup(screen),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )
            _note(mode.grids, fallback, "edit card without preview")
            if fallback.kind is OutcomeKind.APPLIED:
                assert image_url is not None
                mode.grids.refuse_preview(image_url)
