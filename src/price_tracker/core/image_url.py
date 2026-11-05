"""Normalize untrusted product image locations before storing or using them."""

from __future__ import annotations

import ipaddress
import unicodedata
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

MAX_IMAGE_URL_LENGTH: Final = 2048


def normalize_image_url(raw: object, *, base_url: str) -> str | None:
    """The absolute https URL of a product image, or None when ``raw`` is not usable."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip(" \t\r\n\v\f")
    if not raw or any(unicodedata.category(char) == "Cc" or char.isspace() for char in raw):
        return None
    try:
        if raw.startswith("//"):
            raw = "https:" + raw
        elif not urlsplit(raw).scheme:
            base = urlsplit(base_url)
            if base.scheme != "https" or not base.netloc:
                return None
            raw = urljoin(base_url, raw)
        url = httpx.URL(raw)
        if url.scheme != "https":
            return None
        host = url.host
        if not host or "." not in host or host.endswith("."):
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return None
        if url.userinfo or url.port not in (None, 443):
            return None
        url = url.copy_with(fragment=None)
        text = str(url)
        return text if len(text) <= MAX_IMAGE_URL_LENGTH else None
    except (httpx.InvalidURL, ValueError, UnicodeError):
        return None
