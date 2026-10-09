"""Compatibility facade for SQLite repository operations.

Implementation is split into sibling modules by responsibility while the
``price_tracker.db.repository.Repository`` import surface remains stable.
"""

from __future__ import annotations

from price_tracker.db.repository_common import _dec as _dec
from price_tracker.db.repository_digest import DigestRepositoryMixin
from price_tracker.db.repository_history import HistoryRepositoryMixin
from price_tracker.db.repository_ops import OpsRepositoryMixin
from price_tracker.db.repository_products import ProductRepositoryMixin
from price_tracker.db.repository_users import UserRepositoryMixin


class Repository(
    ProductRepositoryMixin,
    UserRepositoryMixin,
    HistoryRepositoryMixin,
    DigestRepositoryMixin,
    OpsRepositoryMixin,
):
    """Typed CRUD wrapper over the SQLite connection."""
