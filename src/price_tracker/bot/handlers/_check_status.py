"""Localized summaries for interactive check outcomes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from price_tracker.bot.messages import _
from price_tracker.bot.ui.check_status import check_reason as check_reason

if TYPE_CHECKING:
    from collections.abc import Sequence

    from price_tracker.core.scheduler import CheckResult
    from price_tracker.db.models import ProductRecord


def check_summary(
    products: Sequence[ProductRecord],
    updated: Sequence[ProductRecord],
    results: Sequence[CheckResult],
) -> list[str]:
    """Show every requested product and distinguish missing or unaccepted results."""
    from price_tracker.bot.ui.product_rows import record_row

    by_id = {result.product_id: result for result in results}
    current = {product["id"]: product for product in updated}
    incomplete = any(
        original["id"] not in by_id or by_id[original["id"]].reason is not None
        for original in products
    )
    heading = (
        _("⚠️ <b>Check finished</b> — {n} products")
        if incomplete
        else _("✅ <b>Check complete</b> — {n} products")
    )
    lines = [heading.format(n=len(products)).replace(" — ", "\n"), ""]
    for original in products:
        product_id = original["id"]
        result = by_id.get(product_id)
        lines.append(record_row(current.get(product_id, original)))
        if result is None or result.reason is not None:
            lines.append(
                _("   {reason}").format(
                    reason=check_reason(result.reason if result is not None else None)
                )
            )
        if result is not None and result.disabled:
            lines.append(_("   Tracking suspended; use /reactivate to resume."))
    return lines
