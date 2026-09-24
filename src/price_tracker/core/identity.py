"""Requested identity and echo comparison for price ownership.

The requested identity comes from the URL the caller asked for — the stored product
URL — and from nothing else. What the page says about itself (the final URL after
redirects, ``<link rel="canonical">``, ``og:url``, node identifiers) is an **echo**: it
is compared with the requested identity and never used as its source. A redirect from
``/p/A`` to ``/p/B`` followed by a page fully consistent with ``B`` is a mismatch, not
a read of ``B``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import tldextract

_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_TRACKING_PARAMS: Final = frozenset({"gclid", "fbclid"})
_TRACKING_PREFIX: Final = "utm_"
_MAX_URL_LENGTH: Final = 4096
ECHO_NAMES: Final = ("final_url", "canonical", "og_url")

_extractor = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def normalize_url(url: object) -> str | None:
    """Normalise an absolute http(s) URL for identity comparison, or return ``None``.

    Scheme and host are lower-cased, the default port, the trailing slash and the
    fragment are removed, tracking parameters (``utm_*``, ``gclid``, ``fbclid``)
    are dropped, and query pairs are stably sorted by key, preserving the order of
    values sharing a key. URLs with
    credentials, without a host, with an invalid port or with control characters are
    not identities.
    """
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LENGTH:
        return None
    if any(ord(c) < 0x21 or ord(c) == 0x7F or 0xD800 <= ord(c) <= 0xDFFF for c in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if (
        scheme not in _ALLOWED_SCHEMES
        or not host
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    netloc = host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
    path = parts.path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in _TRACKING_PARAMS and not key.startswith(_TRACKING_PREFIX)
    ]
    query = urlencode(sorted(query_pairs, key=lambda pair: pair[0]))
    return urlunsplit((scheme, netloc, path, query, ""))


def registrable_domain(normalized_url: str) -> str | None:
    """Registrable domain (public suffix + one label) of a normalised URL."""
    host = urlsplit(normalized_url).hostname or ""
    extracted = _extractor(host)
    domain = extracted.top_domain_under_public_suffix
    return domain or None


@dataclass(frozen=True, slots=True)
class RequestedIdentity:
    """The identity of the product the caller asked for, from the requested URL."""

    url: str  # normalised requested URL
    domain: str  # its registrable domain

    @classmethod
    def from_url(cls, url: str) -> RequestedIdentity:
        """Build the identity from the stored product URL; ``ValueError`` if unusable."""
        normalized = normalize_url(url)
        if normalized is None:
            raise ValueError(f"requested URL {url!r} is not an http(s) identity")
        domain = registrable_domain(normalized)
        if domain is None:
            raise ValueError(f"requested URL {url!r} has no registrable domain")
        return cls(normalized, domain)

    def resolve(self, candidate: str, base: str | None = None) -> str | None:
        """Resolve a possibly relative echo against ``base`` and normalise it."""
        if not isinstance(candidate, str) or not candidate:
            return None
        if any(ord(c) < 0x21 or ord(c) == 0x7F for c in candidate):
            return None
        try:
            joined = urljoin(base or self.url, candidate.strip())
        except ValueError:  # e.g. an unterminated IPv6 literal
            return None
        return normalize_url(joined)


@dataclass(frozen=True, slots=True)
class IdentityCheck:
    """Outcome of the echo comparison; ``error_code`` is ``None`` when consistent."""

    error_code: str | None = None  # "identity_mismatch" | "identity_missing" | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error_code is None


def _mismatch(requested: RequestedIdentity, name: str, value: str) -> IdentityCheck:
    return IdentityCheck("identity_mismatch", f"requested {requested.url} but {name} is {value}")


def check_echoes(
    requested: RequestedIdentity,
    *,
    final_url: str | None = None,
    canonical: str | None = None,
    og_url: str | None = None,
    required: frozenset[str] = frozenset(),
) -> IdentityCheck:
    """Compare every present echo with the requested identity.

    Rules, checked in the fixed order final URL, canonical, ``og:url``; the first
    that fails decides:

    * an echo named in ``required`` and absent → ``identity_missing``;
    * a present echo that cannot be read as an http(s) URL → ``identity_mismatch``;
    * a final URL on another registrable domain → ``identity_mismatch``;
    * a final URL that differs on the same domain is accepted only when the canonical
      equals the requested URL (slug or locale redirect of the same product);
    * a canonical or ``og:url`` that differs from the requested URL → mismatch.
    """
    echoes = {"final_url": final_url, "canonical": canonical, "og_url": og_url}
    for name in ECHO_NAMES:
        if name in required and echoes[name] is None:
            return IdentityCheck("identity_missing", f"required echo {name} is absent")
    base = requested.url
    final_norm: str | None = None
    if final_url is not None:
        final_norm = requested.resolve(final_url)
        if final_norm is None:
            return _mismatch(requested, "final_url", repr(final_url))
        base = final_norm
    canonical_norm: str | None = None
    if canonical is not None:
        canonical_norm = requested.resolve(canonical, base)
        if canonical_norm is None:
            return _mismatch(requested, "canonical", repr(canonical))
    if final_norm is not None and final_norm != requested.url:
        if registrable_domain(final_norm) != requested.domain:
            return _mismatch(requested, "final_url", final_norm)
        if canonical_norm != requested.url:
            return _mismatch(requested, "final_url", final_norm)
    if canonical_norm is not None and canonical_norm != requested.url:
        return _mismatch(requested, "canonical", canonical_norm)
    if og_url is not None:
        og_norm = requested.resolve(og_url, base)
        if og_norm is None:
            return _mismatch(requested, "og_url", repr(og_url))
        if og_norm != requested.url:
            return _mismatch(requested, "og_url", og_norm)
    return IdentityCheck()
