"""Image metadata scoped to the document and identity that supplied a price."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

from price_tracker.core.identity import RequestedIdentity, check_echoes, normalize_url
from price_tracker.core.image_url import normalize_image_url
from price_tracker.core.structured_data import StructureError, decode_json_strict

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

MAX_SCRIPTS = 10
MAX_NODES = 10_000
MAX_CANDIDATES = 20


class ImagePageHTML(str):
    """HTML retaining the final response URL without changing string consumers."""

    final_url: str

    def __new__(cls, text: str, final_url: str) -> ImagePageHTML:
        value = super().__new__(cls, text)
        value.final_url = final_url
        return value


def _payloads(soup: BeautifulSoup) -> tuple[str, ...]:
    return tuple(
        script.get_text()
        for script in soup.find_all(
            "script",
            type=lambda kind: (
                isinstance(kind, str) and kind.strip().casefold() == "application/ld+json"
            ),
            limit=MAX_SCRIPTS + 1,
        )
    )


def _types(node: dict[str, object]) -> frozenset[str] | None:
    if "@type" not in node:
        return frozenset()
    raw = node["@type"]
    values = raw if isinstance(raw, list) else [raw]
    if not values or not all(isinstance(v, str) and v.strip() for v in values):
        return None
    return frozenset(str(v).strip().rstrip("/").rsplit("/", 1)[-1].casefold() for v in values)


@dataclass(slots=True)
class ImageDocument:
    """One bounded parsing, shared with the price selector; references never get renumbered."""

    soup: BeautifulSoup
    payloads: tuple[str, ...]
    nodes: dict[str, dict[str, object]] = field(default_factory=dict)
    valid: bool = True

    @classmethod
    def parse(cls, soup: BeautifulSoup) -> ImageDocument:
        payloads = _payloads(soup)
        doc = cls(soup, payloads)
        if len(payloads) > MAX_SCRIPTS:
            doc.valid = False
            return doc
        for index, raw in enumerate(payloads):
            try:
                root = decode_json_strict(raw)
            except (StructureError, ValueError):
                continue
            stack: list[tuple[object, str, bool]] = [(root, f"jsonld:{index}", True)]
            count = 0
            while stack:
                value, ref, reachable = stack.pop()
                count += 1
                if count > MAX_NODES:
                    doc.valid = False
                    return doc
                if isinstance(value, list):
                    stack.extend(
                        (item, f"{ref}/{i}", reachable)
                        for i, item in reversed(list(enumerate(value)))
                    )
                elif isinstance(value, dict):
                    types = _types(value)
                    if reachable and types is not None:
                        doc.nodes[ref] = value
                    for key, child in reversed(list(value.items())):
                        allowed = key in {"@graph", "mainEntity", "hasVariant"} or (
                            key == "object"
                            and types is not None
                            and any(t.endswith("action") for t in types)
                        )
                        pointer = key.replace("~", "~0").replace("/", "~1")
                        stack.append((child, f"{ref}/{pointer}", reachable and allowed))
        return doc


@dataclass(frozen=True, slots=True)
class PriceImageBinding:
    """Evidence from the price path, never inferred from an image, name or amount.

    References point into ``document`` and denote only the attributed nodes that
    decided the price. ``page_scope`` is set only after checking that the page's
    current product or variant selection represents that same price owner.
    """

    document: ImageDocument
    requested_url: str
    product_refs: tuple[str, ...]
    product_identity: str | None = None
    identity_verified: bool = False
    price_source: Literal["jsonld", "page", "trusted"] = "jsonld"
    variant_refs: tuple[str, ...] = ()
    variant_identity: str | None = None
    product_image_compatible: bool = False
    page_scope: Literal["product", "variant"] | None = None


def _candidates(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            url = item.get("url")
            if not isinstance(url, str):
                url = item.get("contentUrl")
            if isinstance(url, str):
                result.append(url)
    return result


def _identity_url(url: str, variant: str | None) -> str | None:
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    selectors = [value for key, value in pairs if key == "variant"]
    if variant is not None:
        if selectors and selectors != [variant]:
            return None
        parts = parts._replace(query=urlencode([(k, v) for k, v in pairs if k != "variant"]))
    return normalize_url(parts.geturl())


def _echoes_match(
    soup: BeautifulSoup, requested: RequestedIdentity, page_url: str, variant: str | None
) -> bool:
    final_url = _identity_url(page_url, variant)
    if final_url is None or not check_echoes(requested, final_url=final_url).ok:
        return False
    for tag in soup.find_all("link", rel="canonical"):
        href = tag.get("href")
        if not isinstance(href, str):
            return False
        canonical = _identity_url(requested.resolve(href, page_url) or "", variant)
        if canonical is None or not check_echoes(requested, canonical=canonical).ok:
            return False
    for tag in soup.find_all("meta", property="og:url"):
        url = tag.get("content")
        if not isinstance(url, str):
            return False
        og_url = _identity_url(requested.resolve(url, page_url) or "", variant)
        if og_url is None or not check_echoes(requested, og_url=og_url).ok:
            return False
    return True


def _nodes(
    binding: PriceImageBinding, refs: tuple[str, ...], *, variant: bool
) -> list[dict[str, object]] | None:
    result = []
    for ref in refs:
        node = binding.document.nodes.get(ref)
        if node is None or not (_types(node) or frozenset()) & {"product", "productgroup"}:
            return None
        urls = node.get("url")
        for url in urls if isinstance(urls, list) else [urls]:
            if url is None:
                continue
            if not isinstance(url, str):
                return None
            if variant:
                if _identity_url(url, binding.variant_identity) != binding.product_identity:
                    return None
            elif normalize_url(url) != binding.product_identity:
                return None
        result.append(node)
    return result


def _select(groups: list[list[str]], base_url: str) -> tuple[bool, str | None]:
    choices = set()
    for group in groups:
        for raw in group:
            url = normalize_image_url(raw, base_url=base_url)
            if url is not None:
                choices.add(url)
                break
    return len(choices) > 1, next(iter(choices), None)


def extract_page_image(
    soup: BeautifulSoup,
    *,
    page_url: str,
    price_binding: PriceImageBinding | None = None,
) -> str | None:
    """Return only an image attributed by the price path, or no image."""
    binding = price_binding
    if (
        not isinstance(binding, PriceImageBinding)
        or not binding.identity_verified
        or binding.document.soup is not soup
        or not binding.document.valid
        or _payloads(soup) != binding.document.payloads
    ):
        return None
    try:
        identity = _identity_url(binding.requested_url, binding.variant_identity)
        if identity is None or identity != binding.product_identity:
            return None
        requested = RequestedIdentity.from_url(identity)
        if not _echoes_match(soup, requested, page_url, binding.variant_identity):
            return None
        products = _nodes(binding, binding.product_refs, variant=False)
        variants = _nodes(binding, binding.variant_refs, variant=True)
        if products is None or variants is None:
            return None
        variant_groups = [_candidates(node.get("image")) for node in variants]
        product_groups = [_candidates(node.get("image")) for node in products]
        if sum(map(len, variant_groups + product_groups)) > MAX_CANDIDATES:
            return None
        priorities = [variant_groups]
        if binding.variant_identity is None or binding.product_image_compatible:
            priorities.append(product_groups)
        for groups in priorities:
            conflict, chosen = _select(groups, page_url)
            if conflict or chosen is not None:
                return None if conflict else chosen
        if binding.page_scope is None or (
            binding.variant_identity is not None and binding.page_scope != "variant"
        ):
            return None
        for name in ("og:image:secure_url", "og:image"):
            groups = [
                [content]
                for tag in soup.find_all("meta", property=name)
                if isinstance(content := tag.get("content"), str)
            ]
            if sum(map(len, groups)) > MAX_CANDIDATES:
                return None
            conflict, chosen = _select(groups, page_url)
            if conflict or chosen is not None:
                return None if conflict else chosen
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return None
