"""URL parsing utilities."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import tldextract

_extractor = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public destination (SSRF guard)."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for loopback/private/link-local/reserved/multicast/unspecified addresses."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_url(url: str) -> None:
    """Raise :class:`UnsafeURLError` if ``url`` is not a safe public http(s) target.

    SSRF guard for user-supplied product URLs. Blocks non-http(s) schemes and
    hosts that are — or resolve to — loopback/private/link-local/reserved
    addresses (e.g. ``http://localhost``, ``http://127.0.0.1``,
    ``http://169.254.169.254`` cloud-metadata, ``http://192.168.x.x``,
    ``http://[::1]``). An unresolvable host is allowed (it cannot be connected to,
    so it carries no SSRF risk); the scrape simply fails later with a normal error.

    Note: this validates the user-supplied URL at the storage boundary. Redirect
    chains followed at fetch time are a separate, narrower vector and are not
    covered here.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parsed.scheme!r} not allowed")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _is_blocked_ip(literal_ip):
            raise UnsafeURLError(f"host {host} is a non-public address")
        return

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # unresolvable → not reachable → not an SSRF risk
    for info in infos:
        addr = info[4][0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(resolved):
            raise UnsafeURLError(f"host {host} resolves to non-public address {addr}")


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
