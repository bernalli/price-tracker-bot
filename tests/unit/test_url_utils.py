"""Unit tests for url_utils.validate_public_url — SSRF guard (bug #4)."""

from __future__ import annotations

import socket

import pytest

from price_tracker.core.url_utils import UnsafeURLError, validate_public_url


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


def test_validate_public_url_allows_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable host cannot be used for SSRF (no connection); don't block it."""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    validate_public_url("https://does-not-resolve.example/x")  # must not raise
