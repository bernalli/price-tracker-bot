"""Retry policy for HTTP scrapers using tenacity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
    wait_random_exponential,
)

if TYPE_CHECKING:
    from tenacity.wait import wait_base

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class RetryConfig:
    """Configuration for HTTP retry policy."""

    max_attempts: int = 3
    base_wait: float = 1.0
    max_wait: float = 30.0
    jitter: bool = True


# HTTP statuses that are worth retrying (transient server-side or rate limit)
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def is_retryable_http_error(exc: BaseException) -> bool:
    """Return True if `exc` represents a transient HTTP failure worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError))


def with_retry(config: RetryConfig | None = None) -> Callable[[F], F]:
    """Decorate an async function with tenacity retry policy.

    Retries on transient HTTP errors (429, 5xx, timeout, network) up to
    `max_attempts` with exponential backoff. Re-raises the last exception
    if all attempts fail.
    """
    cfg = config or RetryConfig()

    wait_strategy: wait_base
    if cfg.jitter:
        wait_strategy = wait_random_exponential(multiplier=cfg.base_wait, max=cfg.max_wait)
    else:
        wait_strategy = wait_exponential_jitter(initial=cfg.base_wait, max=cfg.max_wait, jitter=0.0)

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(cfg.max_attempts),
                    wait=wait_strategy,
                    retry=retry_if_exception(is_retryable_http_error),
                    reraise=True,
                ):
                    with attempt:
                        return await func(*args, **kwargs)
            except RetryError as e:
                # Should not reach here because reraise=True, but be explicit.
                last_exc = e.last_attempt.exception()
                if last_exc is not None:
                    raise last_exc from e
                raise
            return None  # unreachable; kept for type checker

        return wrapper  # type: ignore[return-value]

    return decorator
