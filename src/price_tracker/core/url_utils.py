"""URL parsing utilities."""

from __future__ import annotations

import tldextract

_extractor = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def extract_etld_plus_one(url: str) -> str:
    """Return the registrable domain (eTLD+1) of a URL.

    Uses the public suffix list to correctly handle multi-part TLDs (.co.uk).
    Returns empty string when the URL has no public suffix or is malformed.
    """
    if not url or not isinstance(url, str):
        return ""
    parts = _extractor(url)
    if not parts.suffix or not parts.domain:
        return ""
    return f"{parts.domain}.{parts.suffix}"
