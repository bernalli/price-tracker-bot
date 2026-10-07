"""Unit tests for url_utils.validate_public_url — SSRF guard (bug #4)."""

from __future__ import annotations

import socket

import pytest
from hypothesis import given
from hypothesis import strategies as st

from price_tracker.core.url_utils import (
    UnsafeURLError,
    UnsupportedSchemeError,
    validate_public_url,
)


def _fake_getaddrinfo(ip: str):
    def _inner(host, port, *args, **kwargs):  # noqa: ANN001, ANN202, ARG001
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80))]

    return _inner


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://192.168.1.10/",
        "http://10.1.2.3/admin",
        "http://172.16.5.4/",
        "http://0.0.0.0/",  # unspecified
        "http://[::1]/",  # IPv6 loopback literal
        "ftp://example.com/file",  # disallowed scheme
        "file:///etc/passwd",  # disallowed scheme
        "https:///no-host",  # missing host
    ],
)
def test_validate_public_url_rejects_unsafe(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/file", "file:///etc/passwd"])
def test_disallowed_scheme_is_an_unsafe_url_with_its_own_type(url: str) -> None:
    with pytest.raises(UnsupportedSchemeError) as excinfo:
        validate_public_url(url)
    assert isinstance(excinfo.value, UnsafeURLError)


UNSUPPORTED_SCHEMES = st.from_regex(r"[A-Za-z][A-Za-z0-9+.-]{0,20}", fullmatch=True).filter(
    lambda scheme: scheme.lower() not in {"http", "https"}
)


@given(scheme=UNSUPPORTED_SCHEMES)
def test_every_non_http_scheme_remains_fail_closed(scheme: str) -> None:
    """Defect: a future scheme branch could bypass the generic SSRF contract."""
    with pytest.raises(UnsupportedSchemeError) as excinfo:
        validate_public_url(f"{scheme}://example.com/product")
    assert isinstance(excinfo.value, UnsafeURLError)


@pytest.mark.parametrize(
    "url",
    [
        " ftp://example.com/file",
        "example.com/product",
        "javascript:alert(1)",
        "data:text/plain,x",
        "http:/x",
        "https:///no-host",
        "://bad",
        "",
    ],
)
def test_not_well_formed_destinations_remain_rejected(url: str) -> None:
    """Defect: malformed or schemeless input must never reach DNS or scraping."""
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


def test_allowed_http_scheme_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect: mixed-case HTTP must not be misclassified as unsupported."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo("93.184.216.34"),
    )
    validate_public_url("hTTp://example.com/product")


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/", "http://169.254.169.254/", "https:///no-host"]
)
def test_non_scheme_rejections_are_not_scheme_errors(url: str) -> None:
    with pytest.raises(UnsafeURLError) as excinfo:
        validate_public_url(url)
    assert not isinstance(excinfo.value, UnsupportedSchemeError)


def test_validate_public_url_rejects_localhost_via_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    with pytest.raises(UnsafeURLError):
        validate_public_url("http://localhost/")


def test_validate_public_url_rejects_host_resolving_to_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("192.168.0.5"))
    with pytest.raises(UnsafeURLError):
        validate_public_url("https://internal.example.com/x")


def test_validate_public_url_allows_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    # Must not raise.
    validate_public_url("https://shop.example/products/widget")


def test_validate_public_url_rejects_public_ipv6_literal() -> None:
    with pytest.raises(UnsafeURLError):
        validate_public_url("http://[2606:4700:4700::1111]/x")


def test_validate_public_url_rejects_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(UnsafeURLError):
        validate_public_url("https://does-not-resolve.example/x")


# --- Shared address space (100.64.0.0/10, RFC 6598) ---------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "100.64.0.0",  # lower bound
        "100.64.0.1",
        "100.100.100.100",
        "100.101.102.103",
        "100.127.255.255",  # upper bound
    ],
)
def test_validate_public_url_rejects_shared_address_space_literal(ip: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_public_url(f"http://{ip}/x")


@pytest.mark.parametrize("ip", ["100.63.255.255", "100.128.0.0"])
def test_validate_public_url_allows_neighbours_of_shared_address_space(ip: str) -> None:
    # Just outside 100.64.0.0/10 on either side: ordinary public addresses.
    validate_public_url(f"http://{ip}/x")  # must not raise


@pytest.mark.parametrize(
    "ip",
    [
        "::ffff:100.64.0.1",  # IPv4-mapped, shared address space
        "::ffff:100.127.255.255",
        "::ffff:127.0.0.1",  # IPv4-mapped, loopback
        "::ffff:10.0.0.1",  # IPv4-mapped, private
        "::127.0.0.1",  # IPv4-compatible, loopback
        "::100.64.0.1",  # IPv4-compatible, shared address space
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "64:ff9b::6440:1",  # NAT64 of 100.64.0.1
    ],
)
def test_validate_public_url_rejects_ipv6_embedding_non_public_ipv4(ip: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_public_url(f"http://[{ip}]/x")


@pytest.mark.parametrize("ip", ["100.64.0.0", "100.101.102.103", "100.127.255.255"])
def test_validate_public_url_rejects_host_resolving_to_shared_address_space(
    monkeypatch: pytest.MonkeyPatch, ip: str
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(ip))
    with pytest.raises(UnsafeURLError):
        validate_public_url("https://shop.example/products/widget")


@pytest.mark.parametrize("ip", ["100.63.255.255", "100.128.0.0"])
def test_validate_public_url_allows_host_resolving_next_to_shared_address_space(
    monkeypatch: pytest.MonkeyPatch, ip: str
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(ip))
    validate_public_url("https://shop.example/products/widget")  # must not raise


@pytest.mark.parametrize("port", [65536, 65537, 99999])
def test_validate_public_url_rejects_out_of_range_port(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    with pytest.raises(UnsafeURLError):
        validate_public_url(f"http://shop.example:{port}/")
