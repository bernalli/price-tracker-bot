"""Tests for tenacity-based retry policy."""

from __future__ import annotations

import httpx
import pytest

from price_tracker.core.retry_policy import (
    RetryConfig,
    is_retryable_http_error,
    with_retry,
)


def test_retry_config_defaults():
    cfg = RetryConfig()
    assert cfg.max_attempts == 3
    assert cfg.base_wait == 1.0
    assert cfg.max_wait == 30.0
    assert cfg.jitter is True


def test_is_retryable_http_error_429():
    err = httpx.HTTPStatusError(
        "429",
        request=httpx.Request("GET", "http://x"),
        response=httpx.Response(429),
    )
    assert is_retryable_http_error(err)


def test_is_retryable_http_error_503():
    err = httpx.HTTPStatusError(
        "503",
        request=httpx.Request("GET", "http://x"),
        response=httpx.Response(503),
    )
    assert is_retryable_http_error(err)


def test_is_retryable_http_error_404_not_retryable():
    err = httpx.HTTPStatusError(
        "404",
        request=httpx.Request("GET", "http://x"),
        response=httpx.Response(404),
    )
    assert not is_retryable_http_error(err)


def test_is_retryable_timeout():
    err = httpx.TimeoutException("timeout")
    assert is_retryable_http_error(err)


def test_is_retryable_network_error():
    err = httpx.ConnectError("connect failed")
    assert is_retryable_http_error(err)


@pytest.mark.asyncio
async def test_with_retry_passes_through_on_success():
    calls = {"n": 0}

    @with_retry(RetryConfig(max_attempts=3, base_wait=0.01, jitter=False))
    async def f() -> str:
        calls["n"] += 1
        return "ok"

    assert await f() == "ok"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_retryable_error():
    calls = {"n": 0}

    @with_retry(RetryConfig(max_attempts=3, base_wait=0.01, jitter=False))
    async def f() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TimeoutException("nope")
        return "ok"

    assert await f() == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_non_retryable():
    calls = {"n": 0}

    @with_retry(RetryConfig(max_attempts=3, base_wait=0.01, jitter=False))
    async def f() -> str:
        calls["n"] += 1
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        await f()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_with_retry_gives_up_after_max_attempts():
    calls = {"n": 0}

    @with_retry(RetryConfig(max_attempts=2, base_wait=0.01, jitter=False))
    async def f() -> str:
        calls["n"] += 1
        raise httpx.TimeoutException("always")

    with pytest.raises(httpx.TimeoutException):
        await f()
    assert calls["n"] == 2
