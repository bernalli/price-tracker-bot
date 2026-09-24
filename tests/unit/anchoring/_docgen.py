"""Document generator for the ownership tests.

A page is described abstractly (:class:`PageSpec`): which node is the requested
product, which nodes are other products, how each is identified, where it sits and
whether its price is readable. :func:`render` turns the description into real JSON-LD
payload texts plus the tier's non-JSON-LD observations (page meta, trusted container,
unscoped attributes). The generator is the oracle's source of truth: it knows which
node is the requested product without ever calling the resolver.

Rendering supports two adversarial knobs used by the not-well-formed tests: object
keys can be emitted in any order, and one object can receive a duplicated key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from price_tracker.core.anchoring import Observation, Ownership
from price_tracker.core.money import Money

if TYPE_CHECKING:
    from decimal import Decimal

REQUESTED_URL = "https://shop.example.com/p/widget-a"
OTHER_URL = "https://shop.example.com/p/other-{index}"
PRODUCT_NAME = "Widget"  # every product shares the name: names never identify a node

# identity modes
URL = "url"  # the node's own product URL (requested URL for the requested node)
URL_RELATIVE = "url_relative"  # the same, written relative to the page
URL_TRACKING = "url_tracking"  # the same, with tracking parameters and a fragment
FRAGMENT = "fragment"  # an @id that only labels the node inside the page
SKU = "sku"  # a SKU, which is a node key but not ownership evidence
NONE = "none"  # no identifier at all

# contexts
TOP = "top"
ITEMLIST = "itemlist"  # member of an ItemList (a carousel)
MAIN_ENTITY = "main_entity"  # WebPage.mainEntity

# price modes
READABLE = "readable"
FINANCING = "financing"
MALFORMED = "malformed"
NO_OFFERS = "no_offers"
MULTI_CURRENCY = "multi_currency"


@dataclass(frozen=True)
class NodeSpec:
    """One product node of the page."""

    index: int
    amount: Decimal
    requested: bool
    identity: str = URL
    context: str = TOP
    price_mode: str = READABLE
    currency: str | None = "EUR"
    duplicated: bool = False


@dataclass(frozen=True)
class PageSpec:
    """A whole page: product nodes plus the tier's other sources."""

    nodes: tuple[NodeSpec, ...]
    layout: str = "scripts"  # "scripts": one script per top element; "graph": one @graph
    page_meta: Decimal | None = None
    trusted: Decimal | None = None
    unscoped: tuple[Decimal, ...] = ()
    key_order_seed: int = 0

    @property
    def requested_node(self) -> NodeSpec | None:
        return next((n for n in self.nodes if n.requested), None)

    @property
    def allowed_amounts(self) -> frozenset[Decimal]:
        """Amounts that belong to the requested product by construction."""
        allowed: set[Decimal] = set()
        node = self.requested_node
        if node is not None:
            allowed.add(node.amount)
        if self.page_meta is not None:
            allowed.add(self.page_meta)
        if self.trusted is not None:
            allowed.add(self.trusted)
        return frozenset(allowed)


def _identity_fields(node: NodeSpec) -> dict[str, Any]:
    own = REQUESTED_URL if node.requested else OTHER_URL.format(index=node.index)
    if node.identity == URL:
        return {"url": own}
    if node.identity == URL_RELATIVE:
        return {"url": own.replace("https://shop.example.com", "")}
    if node.identity == URL_TRACKING:
        return {"url": own + "/?utm_source=feed&gclid=abc#reviews"}
    if node.identity == FRAGMENT:
        return {"@id": f"{REQUESTED_URL}#node-{node.index}"}
    if node.identity == SKU:
        return {"sku": f"SKU-{node.index}"}
    return {}


def _offers(node: NodeSpec) -> dict[str, Any]:
    amount = str(node.amount)
    currency = {"priceCurrency": node.currency} if node.currency else {}
    if node.price_mode == READABLE:
        return {"offers": {"@type": "Offer", "price": amount, **currency}}
    if node.price_mode == FINANCING:
        return {
            "offers": {
                "@type": "Offer",
                "price": amount,
                **currency,
                "priceSpecification": {"@type": "PaymentChargeSpecification"},
            }
        }
    if node.price_mode == MALFORMED:
        return {"offers": {"@type": "Offer", "price": {"amount": amount}, **currency}}
    if node.price_mode == MULTI_CURRENCY:
        return {
            "offers": [
                {"@type": "Offer", "price": amount, "priceCurrency": "EUR"},
                {"@type": "Offer", "price": amount, "priceCurrency": "USD"},
            ]
        }
    return {}


def node_object(node: NodeSpec) -> dict[str, Any]:
    """The JSON-LD object of one product node (without its context wrapper)."""
    return {
        "@type": "Product",
        "name": PRODUCT_NAME,
        **_identity_fields(node),
        **_offers(node),
    }


def _top_elements(page: PageSpec) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    carousel: list[dict[str, Any]] = []
    for node in page.nodes:
        copies = 2 if node.duplicated else 1
        for _ in range(copies):
            obj = node_object(node)
            if node.context == ITEMLIST:
                carousel.append(obj)
            elif node.context == MAIN_ENTITY:
                elements.append({"@type": "WebPage", "url": REQUESTED_URL, "mainEntity": obj})
            else:
                elements.append(obj)
    if carousel:
        items = [
            {"@type": "ListItem", "position": i + 1, "item": obj} for i, obj in enumerate(carousel)
        ]
        elements.append({"@type": "ItemList", "name": "Related", "itemListElement": items})
    return elements


def dumps(
    value: Any,
    *,
    seed: int = 0,
    duplicate_at: int | None = None,
    duplicate_value: Any = None,
    duplicate_first: bool = False,
) -> str:
    """Serialise with a deterministic key order per ``seed``, optionally duplicating a key.

    Objects are numbered in pre-order; the object numbered ``duplicate_at`` receives a
    second member with the name of its first key, holding ``duplicate_value`` (or the
    same value when ``duplicate_value`` is ``None``), before or after the original.
    """
    counter = [0]

    def encode(item: Any, salt: int) -> str:
        if isinstance(item, dict):
            number = counter[0]
            counter[0] += 1
            keys = list(item)
            if seed:
                rotation = (seed + salt) % len(keys) if keys else 0
                keys = keys[rotation:] + keys[:rotation]
                if (seed + salt) % 2:
                    keys.reverse()
            members = [
                f"{json.dumps(k)}:{encode(item[k], salt + i + 1)}" for i, k in enumerate(keys)
            ]
            if number == duplicate_at and keys:
                name = keys[0]
                extra_value = item[name] if duplicate_value is None else duplicate_value
                extra = f"{json.dumps(name)}:{encode(extra_value, salt + 97)}"
                members = [extra, *members] if duplicate_first else [*members, extra]
            return "{" + ",".join(members) + "}"
        if isinstance(item, list):
            return "[" + ",".join(encode(x, salt + i + 1) for i, x in enumerate(item)) + "]"
        return json.dumps(item)

    return encode(value, 0)


def count_objects(value: Any) -> int:
    """Number of JSON objects in ``value`` (the numbering used by :func:`dumps`)."""
    if isinstance(value, dict):
        return 1 + sum(count_objects(v) for v in value.values())
    if isinstance(value, list):
        return sum(count_objects(v) for v in value)
    return 0


@dataclass
class Rendered:
    """A rendered page: JSON-LD payload texts and the tier's other observations."""

    documents: list[Any] = field(default_factory=list)
    payloads: list[str] = field(default_factory=list)
    extra: tuple[Observation, ...] = ()


def render(page: PageSpec) -> Rendered:
    """Render JSON-LD payloads and the non-JSON-LD observations of one tier."""
    elements = _top_elements(page)
    context = {"@context": "https://schema.org"}
    if page.layout == "graph":
        documents: list[Any] = [{**context, "@graph": elements}] if elements else []
    else:
        documents = [{**context, **element} for element in elements]
    payloads = [dumps(doc, seed=page.key_order_seed + i) for i, doc in enumerate(documents)]
    extra: list[Observation] = []
    if page.page_meta is not None:
        extra.append(
            Observation("og", Ownership.PAGE, "meta:og", None, Money(page.page_meta, "EUR"))
        )
    if page.trusted is not None:
        extra.append(
            Observation(
                "container:#buybox",
                Ownership.TRUSTED,
                "container:#buybox",
                None,
                Money(page.trusted, "EUR"),
            )
        )
    for i, amount in enumerate(page.unscoped):
        extra.append(
            Observation(
                "attr:data-price", Ownership.UNSCOPED, f"attr:{i}", None, Money(amount, None)
            )
        )
    return Rendered(documents, payloads, tuple(extra))


@dataclass(frozen=True)
class NestedForeignSpec:
    """A foreign Product under non-owning edges; amounts are distinct by construction."""

    edges: tuple[str, ...]
    types: tuple[str, ...]
    identity: str | None
    reference: str  # absent, resolved, unresolved
    include_requested: bool
    requested_amount: int
    layout: str

    def documents(self, *, live: bool = False) -> list[object]:
        foreign: dict[str, object] = {
            "@type": "Product",
            "offers": {"price": str(self.requested_amount + 100), "priceCurrency": "EUR"},
        }
        if self.identity is not None:
            foreign["url"] = self.identity
        wrapped = foreign
        for edge, node_type in zip(self.edges, self.types, strict=True):
            wrapped = {"@type": node_type, edge: wrapped}
        elements: list[object] = [wrapped]
        if live or self.include_requested or self.reference == "resolved":
            elements.append(
                {
                    "@type": "Product",
                    "@id": "#primary",
                    "url": REQUESTED_URL,
                    "offers": {"price": str(self.requested_amount), "priceCurrency": "EUR"},
                }
            )
        if self.reference != "absent":
            target = "#primary" if live or self.reference == "resolved" else "#missing"
            elements.append({"@type": "WebPage", "mainEntity": {"@id": target}})
        if self.layout == "graph":
            return [{"@graph": elements}]
        return elements
