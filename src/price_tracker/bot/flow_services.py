"""Guided-flow services on the repository for the threshold, target and interval prompts.

:class:`RepositoryFlowServices` implements :class:`~price_tracker.bot.flows.FlowServices`
over the object stored in ``bot_data["db"]``, read on every call because the
repository is created after the handlers are registered. Every mutation re-checks,
when the answer arrives, that the user is still active and can still see the
product: an admin sees every product, any other user only their own.

The add flow is not served here: its four methods raise ``RuntimeError`` without
touching the repository, and the coordinator is registered with the add entry
disabled so they are never reached.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Protocol

from price_tracker.app.inputs import (
    Absolute,
    AnyDrop,
    ClearTarget,
    IntervalMinutes,
    Percentage,
    ResetInterval,
    SetTarget,
)
from price_tracker.bot.flows import (
    AddResult,
    AddScopeDefault,
    ApplyStatus,
    FlowKind,
    PreparedProduct,
    PrepareResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable

NAME_LIMIT: Final = 60
_ADD_NOT_WIRED: Final = "add flow is not wired"


class ProductView(Protocol):
    """Read access to one product row by column name."""

    def get(self, key: str, default: Any = None, /) -> Any: ...


class ProductStore(Protocol):
    """The repository methods these services use."""

    async def is_user_allowed(self, user_id: int) -> bool: ...

    async def is_user_admin(self, user_id: int) -> bool: ...

    async def get_product(self, product_id: int) -> ProductView | None: ...

    async def get_product_for_user(self, product_id: int, user_id: int) -> ProductView | None: ...

    async def set_threshold(
        self, product_id: int, threshold_type: str, threshold_value: Decimal
    ) -> None: ...

    async def set_target_price(self, product_id: int, target: Decimal | None) -> None: ...

    async def set_product_interval(self, product_id: int, minutes: int | None) -> None: ...


class RepositoryFlowServices:
    """:class:`~price_tracker.bot.flows.FlowServices` over the repository."""

    def __init__(self, repository: Callable[[], ProductStore]) -> None:
        self._repository = repository

    async def _visible_product(
        self, store: ProductStore, user_id: int, product_id: int
    ) -> ProductView | None:
        if await store.is_user_admin(user_id):
            return await store.get_product(product_id)
        return await store.get_product_for_user(product_id, user_id)

    async def is_active(self, user_id: int) -> bool:
        """Whether the user may use the bot."""
        return await self._repository().is_user_allowed(user_id)

    async def product_name(self, user_id: int, product_id: int) -> str | None:
        """The product's display name, or ``None`` when the user cannot see it."""
        product = await self._visible_product(self._repository(), user_id, product_id)
        if product is None:
            return None
        name = product.get("name")
        if not isinstance(name, str) or not name:
            return f"#{product_id}"
        return name[:NAME_LIMIT]

    async def apply_value(
        self, user_id: int, kind: FlowKind, product_id: int, value: object
    ) -> ApplyStatus:
        """Write one prompt answer after re-checking access and ownership.

        Raises ``TypeError``, before any write, for a ``(kind, value)`` pair that
        no prompt produces.
        """
        store = self._repository()
        if not await store.is_user_allowed(user_id):
            return ApplyStatus.NOT_AUTHORISED
        if await self._visible_product(store, user_id, product_id) is None:
            return ApplyStatus.NOT_FOUND
        await _write(store, kind, product_id, value)
        return ApplyStatus.OK

    async def prepare_add(self, user_id: int, url: str) -> PrepareResult:
        """Not served: the add flow stays with the legacy handlers."""
        raise RuntimeError(_ADD_NOT_WIRED)

    async def add_product(self, user_id: int, product: PreparedProduct, currency: str) -> AddResult:
        """Not served: the add flow stays with the legacy handlers."""
        raise RuntimeError(_ADD_NOT_WIRED)

    async def add_scope_default(self, user_id: int) -> AddScopeDefault:
        """Not served: the add flow stays with the legacy handlers."""
        raise RuntimeError(_ADD_NOT_WIRED)

    async def set_product_scope(
        self, user_id: int, product_id: int, *, cross_store: bool, scope_override: str | None
    ) -> ApplyStatus:
        """Not served: the add flow stays with the legacy handlers."""
        raise RuntimeError(_ADD_NOT_WIRED)


async def _write(store: ProductStore, kind: FlowKind, product_id: int, value: object) -> None:
    """Apply the one write that ``(kind, type(value))`` maps to; ``TypeError`` otherwise."""
    if kind is FlowKind.THRESHOLD:
        if isinstance(value, Percentage):
            await store.set_threshold(product_id, "percentage", Decimal(value.value))
            return
        if isinstance(value, Absolute):
            await store.set_threshold(product_id, "absolute", value.amount)
            return
        if isinstance(value, AnyDrop):
            await store.set_threshold(product_id, "any_drop", Decimal(0))
            return
    elif kind is FlowKind.TARGET:
        if isinstance(value, SetTarget):
            await store.set_target_price(product_id, value.amount)
            return
        if isinstance(value, ClearTarget):
            await store.set_target_price(product_id, None)
            return
    elif kind is FlowKind.INTERVAL:
        if isinstance(value, IntervalMinutes):
            await store.set_product_interval(product_id, value.minutes)
            return
        if isinstance(value, ResetInterval):
            await store.set_product_interval(product_id, None)
            return
    raise TypeError(f"no write for {kind!r} with {type(value).__name__}")
