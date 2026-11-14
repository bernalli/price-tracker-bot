"""Image downloads use the bounded HTTPS-only public transport."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from price_tracker.core.http_client import build_client
from price_tracker.media import fetch
from price_tracker.media.decode import MAX_IMAGE_BYTES
from price_tracker.media.fetch import ImageFetchError, download_image
from tests.unit.test_public_request_capped import Chunks

URL = "https://images.example.com/photo.jpg"


@pytest.mark.parametrize(
    "content_type",
    [
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/gif",
        "IMAGE/JPEG; charset=binary",
    ],
)
async def test_supported_image_types_and_request_headers(content_type: str) -> None:
    with respx.mock as router:
        route = router.get(URL).respond(
            200, content=b"photo", headers={"Content-Type": content_type}
        )
        async with build_client() as client:
            assert await download_image(URL, client) == b"photo"
        request = route.calls[0].request
        assert request.headers["User-Agent"].startswith("python-httpx/")
        assert request.headers["Accept"] == "image/webp,image/jpeg,image/png,image/gif;q=0.8"
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Accept-Language"] == "en-US,en;q=0.9"
        assert "Referer" not in request.headers
        assert "Authorization" not in request.headers
        assert request.extensions["https_only"] is True
        assert request.extensions["public_deadline_seconds"] <= 20


@pytest.mark.parametrize(
    "url",
    [
        "",
        "/relative.jpg",
        "javascript:alert(1)",
        "http://images.example.com/p.jpg",
        "https://name:password@images.example.com/p.jpg",
        "https://[broken",
        "https://images.example.com:bad/p",
        "https://images.example.com:99999/p",
        "\ud800",
    ],
)
async def test_invalid_urls_cause_no_request(url: str) -> None:
    with respx.mock as router:
        async with build_client() as client:
            with pytest.raises(ImageFetchError, match="invalid url"):
                await download_image(url, client)
        assert len(router.calls) == 0


@pytest.mark.parametrize(
    "content_type", ["", "text/html", "image/svg+xml", "application/octet-stream"]
)
async def test_missing_and_unsupported_content_types_are_refused(content_type: str) -> None:
    with respx.mock as router:
        headers = {"Content-Type": content_type} if content_type else {}
        router.get(URL).respond(200, content=b"photo", headers=headers)
        async with build_client() as client:
            with pytest.raises(ImageFetchError, match="content type"):
                await download_image(URL, client)


async def test_empty_bodies_are_refused() -> None:
    with respx.mock as router:
        router.get(URL).respond(200, content=b"", headers={"Content-Type": "image/jpeg"})
        async with build_client() as client:
            with pytest.raises(ImageFetchError, match="empty"):
                await download_image(URL, client)


@pytest.mark.parametrize("declared", [False, True])
async def test_the_body_cap_is_enforced_and_the_stream_is_closed(declared: bool) -> None:
    stream = Chunks([b"x" * MAX_IMAGE_BYTES, b"x"])
    headers = {"Content-Type": "image/jpeg"}
    if declared:
        headers["Content-Length"] = str(MAX_IMAGE_BYTES + 1)
    with respx.mock as router:
        router.get(URL).respond(200, stream=stream, headers=headers)
        async with build_client() as client:
            with pytest.raises(ImageFetchError, match="too large"):
                await download_image(URL, client)
    assert stream.closed
    assert stream.read is not declared


@pytest.mark.parametrize(
    "destination",
    ["https://127.0.0.1/x.jpg", "https://localhost/x.jpg", "http://images.example.com/x.jpg"],
)
async def test_unsafe_redirects_are_rejected_before_contacting_the_target(destination: str) -> None:
    with respx.mock as router:
        router.get(URL).respond(302, headers={"Location": destination})
        target = router.get(destination).respond(
            200, content=b"photo", headers={"Content-Type": "image/jpeg"}
        )
        async with build_client() as client:
            with pytest.raises(ImageFetchError):
                await download_image(URL, client)
        assert len(target.calls) == 0


@pytest.mark.parametrize("status", [404, 429, 503, 500])
@pytest.mark.parametrize("redirected", [False, True])
async def test_status_metadata_uses_only_the_actual_response_domain(
    status: int, redirected: bool
) -> None:
    final = "https://cdn.other.net/photo.jpg" if redirected else URL
    with respx.mock as router:
        if redirected:
            router.get(URL).respond(302, headers={"Location": final})
        router.get(final).respond(status, headers={"Retry-After": "45"})
        async with build_client() as client:
            with pytest.raises(ImageFetchError, match=f"status {status}") as error:
                await download_image(URL, client)
        assert URL not in str(error.value)
        assert final not in str(error.value)
        if status in (429, 503):
            assert error.value.status_code == status
            assert error.value.response_domain == ("other.net" if redirected else "example.com")
            assert error.value.retry_after_header == "45"
        else:
            assert error.value.response_domain is None
            assert error.value.retry_after_header is None


async def test_encoded_bodies_are_refused_before_reading() -> None:
    stream = Chunks([b"invalid gzip"], forbidden=True)
    with respx.mock as router:
        router.get(URL).respond(
            200, stream=stream, headers={"Content-Encoding": "gzip", "Content-Type": "image/jpeg"}
        )
        async with build_client() as client:
            with pytest.raises(ImageFetchError, match="encoded body"):
                await download_image(URL, client)
    assert stream.closed


async def test_a_transport_error_never_exposes_the_full_url() -> None:
    with respx.mock as router:
        router.get(URL).mock(side_effect=httpx.ConnectError(f"failed {URL}"))
        async with build_client() as client:
            with pytest.raises(ImageFetchError) as error:
                await download_image(URL, client)
        assert URL not in str(error.value)


async def test_a_defensive_check_rejects_an_insecure_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unsafe(*args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"photo",
            headers={"Content-Type": "image/jpeg"},
            request=httpx.Request("GET", URL),
            history=[
                httpx.Response(302, request=httpx.Request("GET", "http://images.example.com/x"))
            ],
        )

    monkeypatch.setattr(fetch, "public_request", unsafe)
    async with build_client() as client:
        with pytest.raises(ImageFetchError, match="insecure redirect"):
            await download_image(URL, client)
