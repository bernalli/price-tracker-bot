"""One diagnostic log line per store refusal: closed fields, sanitized values, no secrets."""

from __future__ import annotations

import ast
import logging
import uuid
from pathlib import Path

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.exceptions import CaptchaDetected, HTTPBlockStatus, WAFBlocked
from price_tracker.core.scraper_base import detect_block_event, refusal_summary

LOGGER = "price_tracker.core.scraper_base"
SRC = Path(__file__).resolve().parents[2] / "src" / "price_tracker"

# The closed marker list, in its documented order, with one body per marker.
MARKER_BODIES: list[tuple[str, str]] = [
    ("local_rate_limited", '{"errors":"local_rate_limited"}'),
    ("verify_connection", "<p>Your connection needs to be verified</p>"),
    ("verify_connection", "<p>Please verify your connection</p>"),
    ("blocked", "<h1>Sorry, you have been blocked</h1>"),
    ("cf_challenge", "<title>Just a moment...</title>"),
    ("cf_challenge", '<div id="cf-chl-widget"></div>'),
    ("cf_challenge", "<script src='/cdn-cgi/challenge-platform/x'></script>"),
    ("captcha_amazon", "<form action='/errors/validateCaptcha'></form>"),
    ("captcha_widget", '<div class="g-recaptcha"></div>'),
    ("captcha_widget", '<div class="h-captcha"></div>'),
    ("captcha_widget", '<div class="cf-turnstile"></div>'),
    # Two markers: the first one in list order wins, wherever it sits in the body.
    ("local_rate_limited", '<div class="g-recaptcha"></div> {"errors":"local_rate_limited"}'),
    ("blocked", "<title>Just a moment...</title> you have been blocked"),
    # No marker at all.
    ("none", "<html>plain refusal</html>"),
]


def _refusal_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and record.getMessage().startswith("store refusal ")
    ]


def _fields(summary: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in summary.split(" "))


def _summary(body: str = "", *, headers: httpx.Headers | None = None, url: str = "") -> str:
    exc = HTTPBlockStatus(status=429, url=url or "https://shop.example/p")
    return refusal_summary(exc, status=429, body=body, headers=headers)


def test_status_refusal_logs_one_closed_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    headers = httpx.Headers(
        {"Server": "cloudflare", "CF-Ray": "abc-MXP", "X-Request-ID": "r-1", "Retry-After": "60"}
    )
    with pytest.raises(HTTPBlockStatus) as exc:
        detect_block_event(
            status_code=429,
            body='{"errors":"local_rate_limited"}',
            url="https://clae.eu/products/x.json?variant=1",
            retry_after="60",
            headers=headers,
        )
    assert exc.value.retry_after == "60"
    records = [r for r in caplog.records if r.name == LOGGER]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == (
        "store refusal reason=HTTP_429 status=429 url=https://clae.eu/products/x.json"
        " server=cloudflare cf_ray=abc-MXP request_id=r-1 retry_after=60"
        " marker=local_rate_limited"
    )


@pytest.mark.parametrize(
    ("status", "body"),
    [(200, "<html><body><h1>Product</h1></body></html>"), (404, "not found"), (500, "oops")],
)
def test_no_line_when_nothing_is_refused(
    caplog: pytest.LogCaptureFixture, status: int, body: str
) -> None:
    caplog.set_level(logging.DEBUG)
    detect_block_event(
        status_code=status,
        body=body,
        url="https://shop.example/p",
        headers=httpx.Headers({"Server": "nginx"}),
    )
    assert _refusal_records(caplog) == []


@pytest.mark.parametrize(("label", "body"), MARKER_BODIES)
def test_markers_are_closed_and_ordered(label: str, body: str) -> None:
    assert _fields(_summary(body))["marker"] == label
    assert _fields(_summary(body.upper()))["marker"] == label


def test_marker_window_is_bounded() -> None:
    assert _fields(_summary("x" * 65536 + "local_rate_limited"))["marker"] == "none"
    assert _fields(_summary("x" * 65518 + "local_rate_limited"))["marker"] == "local_rate_limited"


def _utf8_headers(values: dict[str, str]) -> httpx.Headers:
    # Header values arrive from a store as raw bytes; UTF-8 bytes cover every text.
    return httpx.Headers(
        [(name.encode("ascii"), value.encode("utf-8")) for name, value in values.items()]
    )


@settings(max_examples=200, deadline=None)
@given(
    server=st.text(max_size=1000),
    cf_ray=st.text(max_size=1000),
    request_id=st.text(max_size=1000),
    retry_after=st.text(max_size=1000),
)
def test_hostile_header_values_stay_one_bounded_line(
    server: str, cf_ray: str, request_id: str, retry_after: str
) -> None:
    headers = _utf8_headers(
        {"Server": server, "CF-Ray": cf_ray, "X-Request-ID": request_id, "Retry-After": retry_after}
    )
    summary = _summary("", headers=headers)
    assert "\n" not in summary
    assert "\r" not in summary
    assert all(0x20 <= ord(char) <= 0x7E for char in summary)
    parts = summary.split(" ")
    assert len(parts) == 8
    names = [part.split("=", 1)[0] for part in parts]
    assert names == [
        "reason",
        "status",
        "url",
        "server",
        "cf_ray",
        "request_id",
        "retry_after",
        "marker",
    ]
    for part in parts:
        name, value = part.split("=", 1)
        assert value
        assert len(value) <= (120 if name == "url" else 64)


@settings(max_examples=200, deadline=None)
@given(prefix=st.text(max_size=2000), suffix=st.text(max_size=2000))
def test_body_never_leaks(prefix: str, suffix: str) -> None:
    token = "SENTINEL" + uuid.uuid4().hex
    assert token not in _summary(prefix + token + suffix)


def test_secrets_never_leak() -> None:
    token = "SENTINEL" + uuid.uuid4().hex
    headers = httpx.Headers(
        [
            ("Set-Cookie", f"session={token}"),
            ("Cookie", f"auth={token}"),
            ("Authorization", f"Bearer {token}"),
        ]
    )
    summary = _summary(
        f"<html>{token}</html>",
        headers=headers,
        url="https://user:secretpw@shop.example/p?token=secretq#frag",
    )
    for secret in ("secretpw", "user", "secretq", "frag", token):
        assert secret not in summary
    assert _fields(summary)["url"] == "https://shop.example/p"


def test_missing_and_duplicate_headers() -> None:
    fields = _fields(_summary("", headers=None))
    assert [fields[k] for k in ("server", "cf_ray", "request_id", "retry_after")] == ["-"] * 4

    fields = _fields(_summary("", headers=httpx.Headers({"Server": "nginx", "CF-Ray": "r"})))
    assert fields["server"] == "nginx"
    assert fields["cf_ray"] == "r"
    assert fields["request_id"] == "-"
    assert fields["retry_after"] == "-"

    fields = _fields(_summary("", headers=httpx.Headers([("Server", "a"), ("Server", "b")])))
    assert fields["server"] == "a,_b"


@pytest.mark.parametrize("url", ["", "not a url", "http://[::1", "http://h:99999/"])
def test_unusable_url_never_raises(caplog: pytest.LogCaptureFixture, url: str) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    with pytest.raises(HTTPBlockStatus):
        detect_block_event(status_code=403, body="", url=url, headers=None)
    (line,) = _refusal_records(caplog)
    assert _fields(line.removeprefix("store refusal "))["url"] == "-"


def test_waf_and_captcha_reasons(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    with pytest.raises(WAFBlocked):
        detect_block_event(
            status_code=200, body="<title>Attention Required</title>", url="https://s.example/p"
        )
    with pytest.raises(CaptchaDetected):
        detect_block_event(
            status_code=200, body='<div class="g-recaptcha"></div>', url="https://s.example/p"
        )
    waf, captcha = (
        _fields(line.removeprefix("store refusal ")) for line in _refusal_records(caplog)
    )
    assert waf["reason"] == "WAF/cloudflare"
    assert captcha["reason"] == "CAPTCHA/g-recaptcha"
    assert captcha["marker"] == "captcha_widget"


_EXEMPT_CALLS = {("core/fetch.py", "_fetch_page"), ("scrapers/generic.py", "scrape")}


def _calls_without_headers() -> set[tuple[str, str]]:
    missing: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(SRC).as_posix()

        def visit(node: ast.AST, function: str, rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                name = function
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    name = child.name
                if isinstance(child, ast.Call):
                    func = child.func
                    called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    if called == "detect_block_event" and not any(
                        kw.arg == "headers" for kw in child.keywords
                    ):
                        missing.add((rel, function))
                visit(child, name)

        visit(tree, "<module>")
    return missing


def test_every_response_check_passes_headers() -> None:
    assert _calls_without_headers() == _EXEMPT_CALLS
