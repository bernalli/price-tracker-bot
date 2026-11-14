"""A scripted stand-in for the curl_cffi session that the Chrome store wire opens per hop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from curl_cffi.const import CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests.exceptions import RequestException

if TYPE_CHECKING:
    from collections.abc import Callable

HANG = "hang"
_DEFAULT_CHUNK = 16384


@dataclass
class FakeCurlResponse:
    """One scripted answer; ``primary_ip`` None means the first pinned address.

    ``body`` is always the wire bytes that curl hands to the content callback: with a
    ``Content-Encoding`` of gzip, deflate, br or zstd it must hold that encoding, not
    the decoded text. The fake neither compresses nor decompresses, and
    ``chunk_sizes`` partitions the wire bytes.
    """

    status_code: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    primary_ip: str | None = None
    chunk_sizes: tuple[int, ...] | None = None


class _FakeHeaders:
    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._items = list(items)

    @property
    def raw(self) -> list[tuple[bytes, bytes]]:
        return [(name.encode("ascii"), value.encode("latin-1")) for name, value in self._items]

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self._items)


@dataclass
class _Delivered:
    status_code: int
    headers: _FakeHeaders
    primary_ip: str


def _chunks(body: bytes, sizes: tuple[int, ...] | None) -> list[bytes]:
    if sizes is None:
        return [body[i : i + _DEFAULT_CHUNK] for i in range(0, len(body), _DEFAULT_CHUNK)]
    assert sum(sizes) <= len(body), "chunk sizes exceed the body"
    pieces: list[bytes] = []
    offset = 0
    for size in sizes:
        pieces.append(body[offset : offset + size])
        offset += size
    if offset < len(body):
        pieces.append(body[offset:])
    return pieces


class FakeSession:
    """Records its constructor kwargs and every request; replays the wire's script."""

    def __init__(self, wire: FakeWire, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.requests: list[dict[str, Any]] = []
        self.closes = 0
        self._wire = wire

    @property
    def resolve_entry(self) -> str:
        (entry,) = self.kwargs["curl_options"][CurlOpt.RESOLVE]
        return str(entry)

    @property
    def pinned_addresses(self) -> list[str]:
        return self.resolve_entry.split(":", 2)[2].split(",")

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: list[tuple[str, str]],
        content_callback: Callable[[bytes], object],
        timeout: object,
    ) -> _Delivered:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "content_callback": content_callback,
                "timeout": timeout,
            }
        )
        item = self._wire.next_item()
        if item == HANG:
            await asyncio.Event().wait()
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, FakeCurlResponse)
        for chunk in _chunks(item.body, item.chunk_sizes):
            if content_callback(chunk) == CURL_WRITEFUNC_ERROR:
                raise RequestException("write", code=23)  # type: ignore[arg-type]
        return _Delivered(
            status_code=item.status_code,
            headers=_FakeHeaders(item.headers),
            primary_ip=item.primary_ip if item.primary_ip is not None else self.pinned_addresses[0],
        )

    async def close(self) -> None:
        self.closes += 1


class FakeWire:
    """The sessions opened so far and the script they answer from, in order."""

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.script: list[FakeCurlResponse | BaseException | str] = []
        self.default: FakeCurlResponse | None = None

    def open(self, curl_options: dict[CurlOpt, object]) -> FakeSession:
        session = FakeSession(self, {"curl_options": curl_options})
        self.sessions.append(session)
        return session

    def next_item(self) -> FakeCurlResponse | BaseException | str:
        if self.script:
            return self.script.pop(0)
        assert self.default is not None, "the fake wire ran out of scripted answers"
        return self.default


@pytest.fixture
def fake_wire(monkeypatch: pytest.MonkeyPatch) -> FakeWire:
    """Replace the store wire's session factory with a scripted fake."""
    from price_tracker.core import store_wire  # noqa: PLC0415

    wire = FakeWire()
    monkeypatch.setattr(store_wire, "_open_session", wire.open)
    return wire
