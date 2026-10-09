"""Compatibility facade for scheduler orchestration.

Implementation is split into sibling modules by responsibility while the public
``price_tracker.core.scheduler`` import surface remains stable.
"""

from __future__ import annotations

import asyncio as asyncio  # noqa: TC003 - compatibility re-export
import logging as logging
from contextlib import asynccontextmanager as asynccontextmanager
from dataclasses import dataclass as dataclass
from dataclasses import field as field
from datetime import UTC as UTC
from datetime import datetime as datetime  # noqa: TC003 - compatibility re-export
from datetime import timedelta as timedelta
from typing import TYPE_CHECKING as TYPE_CHECKING
from typing import Any as Any
from typing import Protocol as Protocol
from typing import cast as cast

import httpx as httpx

from price_tracker.bot.messages import reset_locale as reset_locale
from price_tracker.bot.messages import set_locale as set_locale
from price_tracker.bot.messages import user_locale as user_locale
from price_tracker.core.alert import PriceAlert as PriceAlert
from price_tracker.core.alert import ThresholdType as ThresholdType
from price_tracker.core.alert import crosses_threshold as crosses_threshold
from price_tracker.core.alert import format_alert as format_alert
from price_tracker.core.alert import format_back_in_stock as format_back_in_stock
from price_tracker.core.alert import format_operational_notice as format_operational_notice
from price_tracker.core.alert import (
    format_quarantine_notification as format_quarantine_notification,
)
from price_tracker.core.alert import format_warning_notice as format_warning_notice
from price_tracker.core.alert import operational_buttons as operational_buttons
from price_tracker.core.exceptions import LISTING_GONE_STATUSES as LISTING_GONE_STATUSES
from price_tracker.core.exceptions import BlockEvent as BlockEvent
from price_tracker.core.exceptions import ListingGone as ListingGone
from price_tracker.core.exceptions import ParseError as ParseError
from price_tracker.core.health import HealthManager as HealthManager
from price_tracker.core.health import QuarantineState as QuarantineState
from price_tracker.core.notices import NoticeCollector as NoticeCollector
from price_tracker.core.notices import NoticeGroup as NoticeGroup
from price_tracker.core.notices import OperationalEvent as OperationalEvent
from price_tracker.core.notices import group_key_for as group_key_for
from price_tracker.core.outlier import MAX_HELD_READS as MAX_HELD_READS
from price_tracker.core.outlier import REQUIRED_CONFIRMATIONS as REQUIRED_CONFIRMATIONS
from price_tracker.core.outlier import ReadVerdict as ReadVerdict
from price_tracker.core.outlier import classify_read as classify_read
from price_tracker.core.outlier import reads_agree as reads_agree
from price_tracker.core.scheduler_common import CHECK_TICK_MINUTES as CHECK_TICK_MINUTES
from price_tracker.core.scheduler_common import MAX_INTERVAL_MINUTES as MAX_INTERVAL_MINUTES
from price_tracker.core.scheduler_common import CheckResult as CheckResult
from price_tracker.core.scheduler_common import NotifierFn as NotifierFn
from price_tracker.core.scheduler_common import SchedulerDeps as SchedulerDeps  # noqa: TC001
from price_tracker.core.scheduler_common import _failure_reason as _failure_reason
from price_tracker.core.scheduler_notices import NoticeDeliveryMixin as _NoticeDeliveryMixin
from price_tracker.core.scheduler_periodic import PeriodicRunsMixin as _PeriodicRunsMixin
from price_tracker.core.scheduler_products import ProductChecksMixin as _ProductChecksMixin
from price_tracker.core.scraper_base import handle_block_in_pipeline as handle_block_in_pipeline
from price_tracker.core.scraper_base import handle_success_in_pipeline as handle_success_in_pipeline
from price_tracker.core.textlimits import NAME_BUDGET as NAME_BUDGET
from price_tracker.core.textlimits import WHY_BUDGET as WHY_BUDGET
from price_tracker.core.textlimits import truncate_visible as truncate_visible
from price_tracker.core.url_utils import extract_etld_plus_one as extract_etld_plus_one

logger = logging.getLogger(__name__)


class Scheduler(_PeriodicRunsMixin, _ProductChecksMixin, _NoticeDeliveryMixin):
    """Runs price-check sweeps over active products."""

    def __init__(self, deps: SchedulerDeps) -> None:
        self.deps = deps
        # When each product was last attempted by this process, whatever the
        # outcome; run_check_due falls back to the stored timestamps after a restart.
        self._attempted_at: dict[int, datetime] = {}
        self._sold_out_streaks: dict[int, int] = {}
        self._product_locks: dict[int, asyncio.Lock] = {}
