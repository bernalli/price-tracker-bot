"""Untrusted image URL normalization is total, canonical and HTTPS-only."""

from __future__ import annotations

import ipaddress
import unicodedata

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.image_url import MAX_IMAGE_URL_LENGTH, normalize_image_url


@pytest.mark.parametrize(
    ("raw", "base", "expected"),
    [
        (None, "", None),
        (123, "", None),
        ([], "", None),
        ({}, "", None),
        (True, "", None),
        (b"https://cdn.example/x.jpg", "", None),
        ("", "", None),
        ("  ", "", None),
        ("javascript:alert(1)", "", None),
        (" JAVASCRIPT:alert(1)", "", None),
        ("data:image/png;base64,AAAA", "", None),
        ("http://cdn.example/x.jpg", "", None),
        ("ftp://cdn.example/x.jpg", "", None),
        ("file:///x.jpg", "", None),
        ("blob:https://cdn.example/x", "", None),
        ("//cdn.shopify.com/s/x.jpg", "", "https://cdn.shopify.com/s/x.jpg"),
        (
            "/cdn/shop/files/x.jpg",
            "https://clae.eu/products/y",
            "https://clae.eu/cdn/shop/files/x.jpg",
        ),
        ("x.jpg", "http://shop.example/p", None),
        ("x.jpg", "", None),
        ("x.jpg", "https:///", None),
        ("x.jpg", "https://[", None),
        ("https://u:p@cdn.example/x.jpg", "", None),
        ("https://u@cdn.example/x.jpg", "", None),
        ("https://cdn.example:8443/x.jpg", "", None),
        ("https://cdn.example:443/x.jpg", "", "https://cdn.example/x.jpg"),
        ("https://192.0.2.5/x.jpg", "", None),
        ("https://127.1/x.jpg", "", None),
        ("https://0x7f.0.0.1/x.jpg", "", None),
        ("https://1.2.3.4.5/x.jpg", "", None),
        ("https://cdn.0x1f/x.jpg", "", None),
        ("https://[::1]/x.jpg", "", None),
        ("https://localhost/x.jpg", "", None),
        ("https://cdn.example./x.jpg", "", None),
        ("https://cdn.example/a\nb.jpg", "", None),
        ("https://cdn.example/a b.jpg", "", None),
        ("https://cdn.example/a\x7fb.jpg", "", None),
        ("https://cdn.example/a\u2003b.jpg", "", None),
        ("https://cdn.example/x.jpg#frag", "", "https://cdn.example/x.jpg"),
        ("https://bücher.example/x.jpg", "", "https://xn--bcher-kva.example/x.jpg"),
        ("\t https://cdn.example/x.jpg \r\n", "", "https://cdn.example/x.jpg"),
        ("HTTPS://CDN.EXAMPLE/x.jpg", "", "https://cdn.example/x.jpg"),
        ("https://[", "", None),
        ("https://[::1", "", None),
        ("https://cdn.example:abc/x.jpg", "", None),
        ("https://cdn.example:65536/x.jpg", "", None),
        ("https://\ud800.example/x.jpg", "", None),
        ("https://cdn.example/\udfff", "", None),
        ("https://cdn.example/" + "x" * 2030, "", None),
    ],
)
def test_normalization_examples(raw, base, expected):
    assert normalize_image_url(raw, base_url=base) == expected


def test_the_canonical_length_limit_is_inclusive():
    prefix = "https://cdn.example/"
    assert MAX_IMAGE_URL_LENGTH == 2048
    exact = prefix + "a" * (2048 - len(prefix))
    assert normalize_image_url(exact, base_url="") == exact
    assert normalize_image_url(exact + "a", base_url="") is None


@settings(max_examples=300, deadline=None)
@given(
    st.one_of(
        st.text(),
        st.none(),
        st.integers(),
        st.floats(),
        st.binary(),
        st.lists(st.text()),
        st.dictionaries(st.text(), st.text()),
    ),
    st.text(),
)
def test_arbitrary_inputs_are_safe_and_normalization_is_idempotent(raw, base):
    result = normalize_image_url(raw, base_url=base)
    if result is None:
        return
    assert result.startswith("https://")
    assert len(result) <= 2048
    assert not any(c.isspace() or unicodedata.category(c) == "Cc" for c in result)
    url = httpx.URL(result)
    assert not url.fragment
    assert not url.userinfo
    assert url.port in (None, 443)
    assert "." in url.host
    assert not url.host.endswith(".")
    assert not url.host.rsplit(".", 1)[1].isdigit()
    with pytest.raises(ValueError, match=r"."):
        ipaddress.ip_address(url.host)
    assert normalize_image_url(result, base_url="") == result


@settings(max_examples=300, deadline=None)
@given(
    st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")), max_size=40),
    st.sampled_from(["\n", "\0", " ", "\u2003", "\x7f"]),
)
def test_controls_and_whitespace_inside_urls_are_always_rejected(path, hostile):
    assert normalize_image_url(f"https://cdn.example/a{hostile}{path}z", base_url="") is None
