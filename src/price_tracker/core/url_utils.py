"""URL parsing utilities."""

from __future__ import annotations

import ipaddress
import socket

import httpx
import tldextract

_extractor = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public destination (SSRF guard)."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for loopback/private/link-local/reserved/multicast/unspecified/non-global addresses."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global  # e.g. the shared address space of RFC 6598
    )


def validate_public_url(url: str) -> None:
    """Raise :class:`UnsafeURLError` if ``url`` is not a safe public http(s) target.

    SSRF guard for user-supplied product URLs. Blocks non-http(s) schemes and
    IPv6 literals and hosts that are — or resolve to — loopback/private/link-local/reserved
    addresses (e.g. ``http://localhost``, ``http://127.0.0.1``,
    ``http://169.254.169.254`` cloud-metadata, ``http://192.168.x.x``,
    ``http://[::1]``). Hostnames must resolve to at least one public IPv4 address.

    This is an admission check only. The public HTTP transport independently
    validates every request and redirect and connects to the validated address.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise UnsafeURLError("URL is not a valid HTTPX destination") from exc
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parsed.scheme!r} not allowed")
    if not parsed.raw_host:
        raise UnsafeURLError("URL has no host")
    if parsed.port is not None and not 0 <= parsed.port <= 65535:
        raise UnsafeURLError("URL port must be between 0 and 65535")
    host = parsed.raw_host.decode("ascii")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if literal_ip.version != 4:
            raise UnsafeURLError(f"host {host} is an IPv6 address")
        if _is_blocked_ip(literal_ip):
            raise UnsafeURLError(f"host {host} is a non-public address")
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise UnsafeURLError(f"host {host} has no IPv4 address") from exc
    addresses: list[ipaddress.IPv4Address] = []
    for family, _type, _proto, _canonname, sockaddr in infos:
        if family != socket.AF_INET:
            continue
        addr = sockaddr[0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if resolved.version == 4:
            addresses.append(resolved)
    if not addresses:
        raise UnsafeURLError(f"host {host} has no IPv4 address")
    for resolved in addresses:
        if _is_blocked_ip(resolved):
            raise UnsafeURLError(f"host {host} resolves to non-public address {resolved}")


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
