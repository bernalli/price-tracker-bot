"""Strict decoding of JSON-LD payloads into resolver observations.

The decoder is the first place where evidence can be destroyed, so it is strict:

* size (1 MiB per payload) and nesting depth (64) are checked on the raw text
  **before** ``json.loads`` runs, so deep input cannot raise ``RecursionError``;
* a repeated key in any object — equal value or not — rejects the payload: object
  members are never decoded with first-wins or last-wins semantics, because either
  would turn contradictory identity into positive ownership depending on key order;
* ``NaN``/``Infinity`` literals, a BOM, comments, trailing commas and truncation are
  rejections; JSON numbers become ``int`` or ``Decimal``, never ``float``.

A payload that fails any of these checks yields a **terminal** ``AMBIGUOUS
malformed_structure``: no other payload, tier or render stage may rescue the read,
because a malformed payload may be the one that carried the requested product or a
foreign one. The same holds for a node whose own identifiers contradict each other
(``contradictory_identity``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, DecimalException
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

from price_tracker.core.anchoring import (
    AnchorResult,
    AnchorState,
    Observation,
    Ownership,
    resolve_anchor,
)
from price_tracker.core.pricegrammar import PriceContext, Unreadable, select_offer

if TYPE_CHECKING:
    from price_tracker.core.identity import RequestedIdentity

MAX_PAYLOAD_BYTES: Final = 1024 * 1024
MAX_DEPTH: Final = 64
SOURCE: Final = "jsonld"

_PRODUCT_TYPES: Final = frozenset({"product"})
# Explicit schema.org WebPage subtypes; ProductPage is retained as a local alias.
_PAGE_TYPES: Final = frozenset(
    {
        "webpage",
        "itempage",
        "productpage",
        "aboutpage",
        "checkoutpage",
        "contactpage",
        "faqpage",
        "medicalwebpage",
        "profilepage",
        "qapage",
    }
)
_IDENTIFIER_FIELDS: Final = ("sku", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14")
_SCHEMA_PREFIXES: Final = ("https://schema.org/", "http://schema.org/")


class StructureError(ValueError):
    """A payload that cannot be trusted as a whole."""


class _DuplicateKeyError(StructureError):
    pass


class _ContradictoryIdentityError(StructureError):
    pass


def _max_depth(text: str) -> int:
    """Maximum bracket nesting of a JSON text, ignoring brackets inside strings."""
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def _pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKeyError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> object:
    raise StructureError(f"non-finite literal {name}")


def decode_json_strict(text: str) -> object:
    """Decode one JSON payload or raise :class:`StructureError` with the cause."""
    if not isinstance(text, str):
        raise StructureError("payload is not text")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_PAYLOAD_BYTES:
        raise StructureError("payload larger than 1 MiB")
    if _max_depth(text) > MAX_DEPTH:
        raise StructureError(f"payload nested deeper than {MAX_DEPTH}")
    try:
        return json.loads(
            text,
            object_pairs_hook=_pairs_hook,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except StructureError:
        raise
    except (ValueError, RecursionError, DecimalException) as exc:
        raise StructureError(f"not valid JSON: {exc}") from exc


def _type_names(node: Mapping[str, object]) -> frozenset[str]:
    """``@type`` as a set of lower-cased schema names; malformed types raise."""
    raw = node.get("@type")
    if raw is None:
        return frozenset()
    values = raw if isinstance(raw, list) else [raw]
    names: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise StructureError("@type must be a string or a list of strings")
        name = value.strip()
        for prefix in _SCHEMA_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        names.add(name.casefold())
    return frozenset(names)


def _scalar_identifier(node: Mapping[str, object], field: str) -> str | None:
    value = node.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise StructureError(f"{field} must be a string or an integer")
    text = str(value).strip()
    return text or None


def _url_identifiers(node: Mapping[str, object], requested: RequestedIdentity) -> list[str]:
    """Identity-bearing URL fields: ``url`` (one or several) and a fragment-free ``@id``."""
    found: list[str] = []
    url = node.get("url")
    for value in url if isinstance(url, list) else [url]:
        if value is None:
            continue
        if not isinstance(value, str):
            raise StructureError("url must be a string or a list of strings")
        found.append(value)
    node_id = node.get("@id")
    if node_id is not None:
        if not isinstance(node_id, str):
            raise StructureError("@id must be a string")
        if "#" not in node_id or (
            not node_id.startswith("#") and requested.resolve(node_id) != requested.url
        ):
            found.append(node_id)
    return found


@dataclass(frozen=True, slots=True)
class _Frame:
    value: object
    pointer: str
    context: str  # "top" | "page" | "foreign"


def _is_main_page(types: frozenset[str]) -> bool:
    return bool(types & _PAGE_TYPES) and not any(
        marker in name for name in types for marker in ("collection", "list", "searchresults")
    )


def _children(frame: _Frame, types: frozenset[str]) -> Iterator[_Frame]:
    """Assign a position at this edge; ownership never flows down a subtree."""
    value = frame.value
    if isinstance(value, list):
        for index, item in enumerate(value):
            context = frame.context if isinstance(item, Mapping) else "foreign"
            yield _Frame(item, f"{frame.pointer}/{index}", context)
        return
    if not isinstance(value, Mapping):
        return
    for key, item in value.items():
        pointer = f"{frame.pointer}/{key.replace('~', '~0').replace('/', '~1')}"
        context = "foreign"
        if key == "@graph" and frame.context == "top" and not types & _PRODUCT_TYPES:
            context = "top"
        elif (
            key == "mainEntity"
            and frame.context == "top"
            and _is_main_page(types)
            and (not isinstance(item, list) or len(item) == 1)
        ):
            context = "page"
        yield _Frame(item, pointer, context)


def _reference_id(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and set(value) == {"@id"}:
        raw = value["@id"]
        if not isinstance(raw, str):
            raise StructureError("@id must be a string")
        return raw
    return None


def _reference_key(raw: str, requested: RequestedIdentity) -> str:
    """Resolve a node label, retaining its fragment and query spelling."""
    if not raw or any(ord(c) < 0x21 or ord(c) == 0x7F or 0xD800 <= ord(c) <= 0xDFFF for c in raw):
        raise StructureError("unreadable node reference")
    return urljoin(requested.url, raw)


def _product_observation(
    node: Mapping[str, object],
    frame: _Frame,
    payload_index: int,
    requested: RequestedIdentity,
    ctx: PriceContext,
) -> Observation:
    identifiers = _url_identifiers(node, requested)
    resolved_identifiers = {requested.resolve(raw) for raw in identifiers}
    if None in resolved_identifiers:
        raise StructureError("unreadable URL identifier")
    matches = {value == requested.url for value in resolved_identifiers}
    if len(resolved_identifiers) > 1:
        raise _ContradictoryIdentityError(f"node {frame.pointer} names two products")

    if frame.context == "foreign" or matches == {False}:
        ownership = Ownership.FOREIGN
    elif matches == {True}:
        ownership = Ownership.REQUESTED
    elif frame.context == "page":
        ownership = Ownership.PAGE
    else:
        ownership = Ownership.UNKNOWN

    # every identifier field is validated, used or not: a malformed one is terminal
    scalar_ids = [(f, _scalar_identifier(node, f)) for f in _IDENTIFIER_FIELDS]
    node_key: str | None = None
    raw_id = node.get("@id")
    if identifiers:
        node_key = "id:" + (requested.resolve(identifiers[0]) or identifiers[0])
    elif isinstance(raw_id, str) and raw_id.strip():
        try:
            node_key = "ref:" + urljoin(requested.url, raw_id.strip())  # keeps the fragment
        except ValueError:
            node_key = "ref:" + raw_id.strip()
    else:
        for field, value in scalar_ids:
            if value is not None:
                node_key = f"{field.rstrip('0123456789')}:{value}"
                break

    selected = select_offer(node.get("offers"), ctx)
    node_ref = f"{SOURCE}:{payload_index}{frame.pointer}"
    if isinstance(selected, Unreadable):
        return Observation(SOURCE, ownership, node_ref, node_key, None, selected.reason)
    return Observation(SOURCE, ownership, node_ref, node_key, selected)


def jsonld_observations(
    payloads: Sequence[str],
    requested: RequestedIdentity,
    ctx: PriceContext | None = None,
) -> tuple[Observation, ...] | AnchorResult:
    """Emit one observation per Product node of every payload, or a terminal result.

    Only top-level Products and direct non-list WebPage.mainEntity Products are
    candidates. References resolve across all payloads before any price can decide.
    Every other Product is foreign, regardless of its identity.
    """
    context = ctx if ctx is not None else PriceContext()
    observations: list[Observation] = []
    try:
        frames: list[tuple[int, _Frame, frozenset[str]]] = []
        by_id: dict[str, list[int]] = {}
        references: list[str] = []
        for index, payload in enumerate(payloads):
            stack = [_Frame(decode_json_strict(payload), "", "top")]
            while stack:
                frame = stack.pop()
                types: frozenset[str] = frozenset()
                if isinstance(frame.value, Mapping):
                    types = _type_names(frame.value)
                    if frame.context == "top" and "@id" in frame.value:
                        raw_id = frame.value["@id"]
                        if not isinstance(raw_id, str):
                            raise StructureError("@id must be a string")
                        key = _reference_key(raw_id, requested)
                        by_id.setdefault(key, []).append(len(frames))
                frames.append((index, frame, types))
                if frame.context == "page":
                    # A singleton array is one MAIN value, including a string reference.
                    value = frame.value
                    if isinstance(value, list) and len(value) == 1:
                        value = value[0]
                    ref = _reference_id(value)
                    if ref is not None:
                        references.append(_reference_key(ref, requested))
                stack.extend(_children(frame, types))

        promoted: set[int] = set()
        unresolved = False
        for ref in references:
            targets = by_id.get(ref, [])
            if len(targets) != 1 or not frames[targets[0]][2] & _PRODUCT_TYPES:
                unresolved = True
            else:
                promoted.add(targets[0])
        for number, (index, frame, types) in enumerate(frames):
            if types & _PRODUCT_TYPES and isinstance(frame.value, Mapping):
                node = frame.value
                if number in promoted:
                    frame = replace(frame, context="page")
                observations.append(_product_observation(node, frame, index, requested, context))
        if unresolved:
            return AnchorResult(AnchorState.AMBIGUOUS, reason="unresolved_main_entity")
    except _ContradictoryIdentityError:
        return AnchorResult(AnchorState.AMBIGUOUS, reason="contradictory_identity")
    except (StructureError, ValueError, UnicodeError):
        return AnchorResult(AnchorState.AMBIGUOUS, reason="malformed_structure")
    return tuple(observations)


def anchor_structured_page(
    payloads: Sequence[str],
    requested: RequestedIdentity,
    *,
    extra: tuple[Observation, ...] = (),
    ctx: PriceContext | None = None,
) -> AnchorResult:
    """Tier A over JSON-LD payloads plus observations of other sources of the same tier.

    ``extra`` carries the tier's other observations (page metas as ``PAGE``, trusted
    containers as ``TRUSTED``, diagnostic selectors as ``UNSCOPED``). A terminal
    structural rejection is returned as is: nothing in ``extra`` can rescue it.
    """
    extracted = jsonld_observations(payloads, requested, ctx)
    if isinstance(extracted, AnchorResult):
        return extracted
    return resolve_anchor(extracted + extra)
