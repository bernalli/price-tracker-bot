"""Price ownership: the tri-state resolver.

Sources (JSON-LD, microdata, page metas, trusted containers, platform APIs, selectors)
never decide. Each emits one :class:`Observation` per product node it sees — including
nodes without a readable price — and :func:`resolve_anchor` decides, over one tier's
observations, whether the requested product's price is ``FOUND``, whether there is
``NO_CANDIDATE``, or whether the page is ``AMBIGUOUS``. ``AMBIGUOUS`` halts the chain.

Invariants enforced here and pinned by the property tests:

* an unscoped observation never produces ``FOUND``;
* an observation owned by the requested product that has no readable price is final
  (``NO_CANDIDATE``); no other node substitutes it;
* any source attributed to the requested product (identity match, trusted container,
  page-level declaration) must agree with every other attributed source, or there is
  no price;
* a readable unidentified node vetoes an attributed price it disagrees with on
  currency or amount; it never joins the attributed price and never supplies one of
  its own to it;
* a foreign observation with no attributed one is ``AMBIGUOUS``, never a price;
* nodes are grouped by node identity, never by amount: two unidentified nodes are two
  nodes even when their prices are equal;
* the outcome is invariant under every permutation and duplication of the
  observations, and malformed states cannot be constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final

from price_tracker.core.money import Money, currency_precision, significant_fraction_digits
from price_tracker.core.pricegrammar import UNREADABLE_REASONS

__all__ = [
    "MAX_OBSERVATIONS",
    "REASONS_BY_STATE",
    "AnchorResult",
    "AnchorState",
    "Money",
    "Observation",
    "Ownership",
    "resolve_anchor",
]

MAX_OBSERVATIONS: Final = 200
MAX_LABEL_LENGTH: Final = 2048


class Ownership(Enum):
    """Why an observation may (or may not) belong to the requested product."""

    REQUESTED = "requested"  # node identifier equals the requested identity
    TRUSTED = "trusted"  # inside a container id declared by the adapter, fixture-verified
    PAGE = "page"  # og:/product: metas, WebPage.mainEntity without an identifier
    UNKNOWN = "unknown"  # a product node with no identifier
    FOREIGN = "foreign"  # identifier differs, or carousel / related / ItemList context
    UNSCOPED = "unscoped"  # generic selector or data attribute; never decides


class AnchorState(Enum):
    """The three outcomes of price ownership."""

    FOUND = "found"
    NO_CANDIDATE = "no_candidate"
    AMBIGUOUS = "ambiguous"


# Closed vocabulary; the metric label ``anchor_reason`` uses it.
REASONS_BY_STATE: Final[dict[AnchorState, frozenset[str]]] = {
    AnchorState.FOUND: frozenset({"owned", "attributed", "singleton_unknown"}),
    AnchorState.NO_CANDIDATE: frozenset(
        {
            "requested_unreadable",
            "attributed_unreadable",
            "unknown_unreadable",
            "unscoped_only",
            "no_observation",
        }
    ),
    AnchorState.AMBIGUOUS: frozenset(
        {
            "owned_sources_disagree",
            "attributed_sources_disagree",
            "attributed_unknown_disagree",
            "currencies_disagree",
            "foreign_only",
            "unknown_node_disagrees",
            "multiple_unknown_nodes",
            "too_many_nodes",
            "malformed_structure",
            "unresolved_main_entity",
            "contradictory_identity",
        }
    ),
}

_ATTRIBUTED: Final = frozenset({Ownership.TRUSTED, Ownership.PAGE})


def _check_label(name: str, value: object, *, optional: bool) -> None:
    if value is None and optional:
        return
    if type(value) is not str:
        raise TypeError(f"{name} must be a str, got {type(value).__name__}")
    if not value or len(value) > MAX_LABEL_LENGTH:
        raise ValueError(f"{name} must be a non-empty string of at most {MAX_LABEL_LENGTH}")


@dataclass(frozen=True, slots=True)
class Observation:
    """One product node seen by one source.

    ``node_ref`` locates the node inside the fetched document (for example
    ``"jsonld:0/@graph/2"``); it is unique per node and identical when the same node is
    emitted twice. ``node_key`` is the node's identity (URL, ``@id``, SKU, MPN, GTIN) —
    never its name and never its price. ``price is None`` means the node is present but
    its price is unreadable, and ``unreadable_reason`` says why.
    """

    source: str
    ownership: Ownership
    node_ref: str
    node_key: str | None
    price: Money | None
    unreadable_reason: str = ""

    def __post_init__(self) -> None:
        _check_label("source", self.source, optional=False)
        if not isinstance(self.ownership, Ownership):
            raise TypeError("ownership must be an Ownership")
        _check_label("node_ref", self.node_ref, optional=False)
        _check_label("node_key", self.node_key, optional=True)
        if self.price is not None and not isinstance(self.price, Money):
            raise TypeError("price must be a Money or None")
        if type(self.unreadable_reason) is not str:
            raise TypeError("unreadable_reason must be a str")
        if self.price is None and self.unreadable_reason not in UNREADABLE_REASONS:
            raise ValueError("an observation without a price must name an unreadable reason")
        if self.price is not None and self.unreadable_reason:
            raise ValueError("an observation with a price cannot carry an unreadable reason")


@dataclass(frozen=True, slots=True)
class AnchorResult:
    """The resolver's decision. Inconsistent combinations cannot be constructed."""

    state: AnchorState
    price: Money | None = None
    reason: str = ""
    deciding: tuple[Observation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, AnchorState):
            raise TypeError("state must be an AnchorState")
        if type(self.reason) is not str or self.reason not in REASONS_BY_STATE[self.state]:
            raise ValueError(f"reason {self.reason!r} is not valid for {self.state.value}")
        if self.state is AnchorState.FOUND:
            if not isinstance(self.price, Money):
                raise ValueError("FOUND requires a price")
        elif self.price is not None:
            raise ValueError(f"{self.state.value} cannot carry a price")
        if type(self.deciding) is not tuple or not all(
            isinstance(o, Observation) for o in self.deciding
        ):
            raise TypeError("deciding must be a tuple of Observation")


def _sort_key(obs: Observation) -> tuple[str, str, str, str, Decimal, str, str]:
    price = obs.price
    return (
        obs.source,
        obs.node_ref,
        obs.ownership.value,
        obs.node_key or "",
        Decimal(0) if price is None else price.amount,
        "" if price is None or price.currency is None else price.currency,
        obs.unreadable_reason,
    )


def _canonical(observations: frozenset[Observation]) -> tuple[Observation, ...]:
    return tuple(sorted(observations, key=_sort_key))


def _decide(
    deciding: frozenset[Observation], found_reason: str, disagree_reason: str
) -> AnchorResult | None:
    """Unify currencies and amounts of the readable members; ``None`` if none readable."""
    readable = [o.price for o in deciding if o.price is not None]
    if not readable:
        return None
    ordered = _canonical(deciding)
    currencies = {m.currency for m in readable if m.currency is not None}
    if len(currencies) > 1:
        return AnchorResult(AnchorState.AMBIGUOUS, reason="currencies_disagree", deciding=ordered)
    amounts = {m.amount for m in readable}
    if len(amounts) > 1:
        return AnchorResult(AnchorState.AMBIGUOUS, reason=disagree_reason, deciding=ordered)
    currency = currencies.pop() if currencies else None
    amount = min(readable, key=lambda m: str(m.amount)).amount
    if currency is not None and significant_fraction_digits(amount) > currency_precision(currency):
        return AnchorResult(AnchorState.AMBIGUOUS, reason="currencies_disagree", deciding=ordered)
    return AnchorResult(
        AnchorState.FOUND, price=Money(amount, currency), reason=found_reason, deciding=ordered
    )


def _attributed_unknown_veto(
    attributed_result: AnchorResult,
    unknown: frozenset[Observation],
    attributed: frozenset[Observation],
) -> AnchorResult | None:
    """Rule 2's veto: a readable unidentified node may reject the attributed price.

    It never joins it: an unknown node with no readable price, or none at all,
    changes nothing. ``None`` means the attributed result stands unchanged.
    """
    price = attributed_result.price
    assert price is not None  # attributed_result is FOUND
    readable = frozenset(o for o in unknown if o.price is not None)
    if not readable:
        return None
    deciding = _canonical(attributed | unknown)
    for obs in readable:
        assert obs.price is not None
        if (
            obs.price.currency is not None
            and price.currency is not None
            and obs.price.currency != price.currency
        ):
            return AnchorResult(
                AnchorState.AMBIGUOUS, reason="currencies_disagree", deciding=deciding
            )
    for obs in readable:
        assert obs.price is not None
        if obs.price.amount != price.amount:
            return AnchorResult(
                AnchorState.AMBIGUOUS, reason="attributed_unknown_disagree", deciding=deciding
            )
    return None


def _contradiction(observations: frozenset[Observation]) -> bool:
    """True when one node (same ``node_ref``) is described with two identities."""
    seen: dict[str, tuple[str, Ownership, str | None]] = {}
    for obs in observations:
        identity = (obs.source, obs.ownership, obs.node_key)
        previous = seen.setdefault(obs.node_ref, identity)
        if previous != identity:
            return True
    return False


def resolve_anchor(observations: tuple[Observation, ...]) -> AnchorResult:
    """Decide FOUND / NO_CANDIDATE / AMBIGUOUS over one tier's observations.

    One precedence, first match wins: structural checks (too many nodes, one node with
    two identities), then owned, attributed, foreign-only, singleton unknown,
    unscoped-only, nothing. Within the attributed step, a readable unidentified node
    can veto an attributed price it disagrees with (on currency or amount), turning a
    would-be ``FOUND`` into ``AMBIGUOUS``; it never contributes to or replaces that
    price.
    """
    if type(observations) is not tuple:
        raise TypeError("observations must be a tuple")
    for obs in observations:
        if not isinstance(obs, Observation):
            raise TypeError(f"not an Observation: {type(obs).__name__}")

    distinct = frozenset(observations)  # duplicates of one observation are one observation
    everything = _canonical(distinct)
    if len(distinct) > MAX_OBSERVATIONS:
        return AnchorResult(AnchorState.AMBIGUOUS, reason="too_many_nodes")
    if _contradiction(distinct):
        return AnchorResult(
            AnchorState.AMBIGUOUS, reason="contradictory_identity", deciding=everything
        )

    by_ownership: dict[Ownership, frozenset[Observation]] = {
        kind: frozenset(o for o in distinct if o.ownership is kind) for kind in Ownership
    }
    requested = by_ownership[Ownership.REQUESTED]
    attributed = by_ownership[Ownership.TRUSTED] | by_ownership[Ownership.PAGE]

    # 1. Owned: every source attributed to the requested product must agree.
    if requested:
        deciding = requested | attributed
        if not any(o.price is not None for o in requested):
            return AnchorResult(
                AnchorState.NO_CANDIDATE,
                reason="requested_unreadable",
                deciding=_canonical(deciding),
            )
        result = _decide(deciding, "owned", "owned_sources_disagree")
        if result is None:  # pragma: no cover - requested has a readable member
            raise AssertionError("unreachable")
        return result

    # 2. Attributed: trusted containers and page-level declarations, together.
    if attributed:
        result = _decide(attributed, "attributed", "attributed_sources_disagree")
        if result is None:
            return AnchorResult(
                AnchorState.NO_CANDIDATE,
                reason="attributed_unreadable",
                deciding=_canonical(attributed),
            )
        if result.state is AnchorState.FOUND:
            veto = _attributed_unknown_veto(result, by_ownership[Ownership.UNKNOWN], attributed)
            if veto is not None:
                return veto
        return result

    # 3. Foreign only: someone else's price is not an unambiguous price.
    foreign = by_ownership[Ownership.FOREIGN]
    if foreign:
        return AnchorResult(
            AnchorState.AMBIGUOUS, reason="foreign_only", deciding=_canonical(foreign)
        )

    # 4. Singleton unknown: grouped by node identity, never by amount.
    unknown = by_ownership[Ownership.UNKNOWN]
    if unknown:
        groups = {
            ("key", o.node_key) if o.node_key is not None else ("ref", o.node_ref) for o in unknown
        }
        if len(groups) > 1:
            return AnchorResult(
                AnchorState.AMBIGUOUS,
                reason="multiple_unknown_nodes",
                deciding=_canonical(unknown),
            )
        result = _decide(unknown, "singleton_unknown", "unknown_node_disagrees")
        if result is not None:
            return result
        return AnchorResult(
            AnchorState.NO_CANDIDATE, reason="unknown_unreadable", deciding=_canonical(unknown)
        )

    # 5. Unscoped only: kept for the diagnostic, never a price.
    unscoped = by_ownership[Ownership.UNSCOPED]
    if unscoped:
        return AnchorResult(
            AnchorState.NO_CANDIDATE, reason="unscoped_only", deciding=_canonical(unscoped)
        )

    # 6. Nothing.
    return AnchorResult(AnchorState.NO_CANDIDATE, reason="no_observation")
