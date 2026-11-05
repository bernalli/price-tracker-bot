"""Download images through the bounded, validated public HTTPS transport."""

from __future__ import annotations

import random
from typing import Final

import httpx

from price_tracker.core.http_client import BodyLimitError, public_request
from price_tracker.core.image_url import normalize_image_url
from price_tracker.core.scraper_base import USER_AGENTS
from price_tracker.core.url_utils import UnsafeURLError, extract_etld_plus_one
from price_tracker.media.decode import MAX_IMAGE_BYTES

IMAGE_TYPES: Final = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"})
DOWNLOAD_TIMEOUT: Final = 20.0


class ImageFetchError(Exception):
    """An image download was refused or failed; ``reason`` says why (never the full URL)."""

    def __init__(
        self,
        reason: str,
        *,
        status_code: int | None = None,
        response_domain: str | None = None,
        retry_after_header: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.response_domain = response_domain
        self.retry_after_header = retry_after_header


async def download_image(url: str, client: httpx.AsyncClient) -> bytes:
    if normalize_image_url(url, base_url=url) != url:
        raise ImageFetchError("invalid url")
    try:
        response = await public_request(
            client,
            "GET",
            url,
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "image/webp,image/jpeg,image/png,image/gif;q=0.8",
                "Accept-Encoding": "identity",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=DOWNLOAD_TIMEOUT,
            max_body_bytes=MAX_IMAGE_BYTES,
            https_only=True,
        )
    except BodyLimitError as exc:
        raise ImageFetchError(exc.reason) from exc
    except (UnsafeURLError, httpx.HTTPError) as exc:
        raise ImageFetchError(type(exc).__name__) from exc
    if response.status_code != 200:
        metadata = response.status_code in (429, 503)
        raise ImageFetchError(
            f"status {response.status_code}",
            status_code=response.status_code if metadata else None,
            response_domain=extract_etld_plus_one(str(response.url)) if metadata else None,
            retry_after_header=response.headers.get("Retry-After") if metadata else None,
        )
    if any(item.url.scheme != "https" for item in (*response.history, response)):
        raise ImageFetchError("insecure redirect")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in IMAGE_TYPES:
        raise ImageFetchError("content type")
    if not response.content:
        raise ImageFetchError("empty")
    return response.content
