"""Property tests for the public-address admission and connection boundary."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from price_tracker.core.http_client import _resolve_public
from price_tracker.core.url_utils import UnsafeURLError, _is_blocked_ip, validate_public_url

SPECIAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)

HOST_FORMS = (
    "shop.example",
    "SHOP.EXAMPLE",
    "shop.example.",
    "SHOP.EXAMPLE.",
    "bücher.example",
    "BÜCHER.EXAMPLE",
    "xn--bcher-kva.example",
    "xn--bcher-kva.example.",
)

PUBLIC_IPV4S = st.ip_addresses(v=4).filter(lambda address: not _is_blocked_ip(address))


def _literal_url(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}/product"


@settings(max_examples=256, deadline=None)
@given(address=st.ip_addresses())
async def test_literal_acceptance_implies_public_ipv4_connection(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    url = _literal_url(address)
    allowed = address.version == 4 and not _is_blocked_ip(address)
    if not allowed:
        with pytest.raises(UnsafeURLError):
            validate_public_url(url)
        with pytest.raises(UnsafeURLError):
            await _resolve_public(httpx.URL(url))
        return

    validate_public_url(url)
    assert await _resolve_public(httpx.URL(url)) == (str(address),)


@pytest.mark.parametrize("network", SPECIAL_NETWORKS, ids=str)
@settings(max_examples=16, deadline=None)
@given(offset=st.integers(min_value=0, max_value=(1 << 128) - 1))
async def test_special_ranges_follow_the_literal_policy(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
    offset: int,
) -> None:
    address = network.network_address + offset % network.num_addresses
    url = _literal_url(address)
    allowed = address.version == 4 and not _is_blocked_ip(address)
    if allowed:
        validate_public_url(url)
        assert await _resolve_public(httpx.URL(url)) == (str(address),)
    else:
        with pytest.raises(UnsafeURLError):
            validate_public_url(url)
        with pytest.raises(UnsafeURLError):
            await _resolve_public(httpx.URL(url))


def _numeric_hosts(address: ipaddress.IPv4Address) -> tuple[str, ...]:
    octets = tuple(int(part) for part in str(address).split("."))
    value = int(address)
    return (
        str(address),
        str(value),
        f"0x{value:x}",
        f"0{value:o}",
        ".".join(f"0x{octet:x}" for octet in octets),
        ".".join(f"0{octet:o}" for octet in octets),
    )


@settings(
    max_examples=128,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(address=PUBLIC_IPV4S)
async def test_accepted_numeric_host_encoding_is_dialable(
    monkeypatch: pytest.MonkeyPatch,
    address: ipaddress.IPv4Address,
) -> None:
    def resolve(_host: str, port: int, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (str(address), port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    accepted = 0
    for host in _numeric_hosts(address):
        raw_url = f"http://{host}/product"
        try:
            validate_public_url(raw_url)
        except UnsafeURLError:
            continue
        accepted += 1
        url = httpx.URL(raw_url)
        assert await _resolve_public(url) == (str(address),)
    assert accepted >= 1


@settings(
    max_examples=128,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(answers=st.lists(st.ip_addresses(), max_size=8))
async def test_dns_acceptance_implies_only_public_ipv4_connections(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> None:
    calls: list[tuple[str, int | None]] = []

    def resolve(host: str, port: int, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        family = kwargs.get("family", args[0] if args else None)
        calls.append((host, family))
        return [
            (
                socket.AF_INET if address.version == 4 else socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (str(address), port),
            )
            for address in answers
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    ipv4_answers = [address for address in answers if address.version == 4]
    allowed = bool(ipv4_answers) and not any(_is_blocked_ip(address) for address in ipv4_answers)
    url = "https://shop.example/product"
    if not allowed:
        with pytest.raises(UnsafeURLError):
            validate_public_url(url)
        with pytest.raises(UnsafeURLError):
            await _resolve_public(httpx.URL(url))
    else:
        validate_public_url(url)
        expected = tuple(dict.fromkeys(str(address) for address in ipv4_answers))
        assert await _resolve_public(httpx.URL(url)) == expected
        assert all(
            isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address)
            and not _is_blocked_ip(ipaddress.ip_address(address))
            for address in expected
        )
    assert calls
    assert all(family == socket.AF_INET for _host, family in calls)


@pytest.mark.parametrize("host", HOST_FORMS)
@settings(
    max_examples=16,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(address=PUBLIC_IPV4S)
async def test_hostname_normalizations_preserve_public_ipv4_connection(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    address: ipaddress.IPv4Address,
) -> None:
    families: list[int | None] = []

    def resolve(_host: str, port: int, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        families.append(kwargs.get("family", args[0] if args else None))
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (str(address), port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    raw_url = f"https://{host}/product"
    validate_public_url(raw_url)
    assert await _resolve_public(httpx.URL(raw_url)) == (str(address),)
    assert families == [socket.AF_INET, socket.AF_INET]
