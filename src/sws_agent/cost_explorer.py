"""Account-level cost collection from AWS Cost Explorer (M2C-E).

Fresh SWS module (no SMS reuse). Collects deterministic, workspace-level
cost estimates through an injected client using the same hermetic pattern as
inventory.py: no boto3 dependency and no live AWS calls in this milestone.

Honesty contract: Cost Explorer returns account-level aggregated data, never
per-resource figures. This collector therefore NEVER fabricates per-resource
costs; every estimate is either the account total or an aggregate grouped by
one canonical dimension/tag key. Negative credit entries cannot be represented
by ``CostEstimate`` (``amount_usd`` is bounded ``ge=0.0``), so they are
excluded from aggregation and the exclusion is surfaced as an assumption.

Scope (approved M2C-E):
  - Units: ``UnblendedCost`` at daily granularity over an explicit window.
  - ``end_date`` is required and explicit; there is no current-time dependence,
    so repeated runs are deterministic testable facts.
  - ``window_days`` is bounded to 1..``MAX_COST_WINDOW_DAYS``; invalid values
    are rejected, never silently expanded.
  - ``group_by`` accepts only ``SWS_SUPPORTED_COST_GROUP_BY_KEYS``, at most
    ``MAX_COST_GROUP_BY_KEYS`` entries; anything else is rejected.
  - The total query is the primary operation: its failure is a PRIMARY failure
    and collection fails closed (returns ``[]``). Each grouped query is an
    enrichment operation: its failure is traced as ENRICHMENT and collection
    still returns whatever succeeded. Unparseable amounts are PARSE failures
    and the affected entries are skipped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .constants import (
    MAX_COST_GROUP_BY_KEYS,
    MAX_COST_WINDOW_DAYS,
    COST_GROUP_DIMENSION_SERVICE,
    COST_GROUP_TAG_OWNER,
    CollectionFailureCategory,
    SWSResourceType,
    SWS_SUPPORTED_COST_GROUP_BY_KEYS,
    TraceEventType,
)
from .inventory import _InventoryCollectorBase
from .interfaces import TraceSink
from .models import CostEstimate

CREDIT_EXCLUSION_ASSUMPTION: str = (
    "negative credit entries were excluded from the aggregated amount"
)
"""Surfaced assumption whenever negative (credit) entries were skipped."""

_COST_GROUP_BY_PARAMS: dict[str, dict[str, str]] = {
    COST_GROUP_DIMENSION_SERVICE: {"Type": "DIMENSION", "Key": "SERVICE"},
    COST_GROUP_TAG_OWNER: {"Type": "TAG", "Key": "Owner"},
}
"""Client-side GroupBy translation for each supported group-by key."""


def normalize_window_days(window_days: int | None) -> int:
    """Bound a caller-provided cost window to the canonical cap.

    ``None`` means the canonical maximum (matching the ``config.py``
    default). Zero, negative, non-integer, and over-cap values are rejected;
    invalid requests are never silently expanded.
    """
    if window_days is None:
        return MAX_COST_WINDOW_DAYS
    if isinstance(window_days, bool) or not isinstance(window_days, int):
        raise ValueError("window_days must be a positive integer or None")
    if window_days < 1 or window_days > MAX_COST_WINDOW_DAYS:
        raise ValueError(
            f"window_days must be between 1 and {MAX_COST_WINDOW_DAYS}"
        )
    return window_days


def normalize_group_by(group_by: list[str] | None) -> list[str]:
    """Validate a requested cost group-by key list.

    Accepts only keys in ``SWS_SUPPORTED_COST_GROUP_BY_KEYS``, without
    duplicates, and at most ``MAX_COST_GROUP_BY_KEYS`` entries. ``None`` and
    an empty list mean "no grouping". Unknown keys, non-strings, duplicates,
    and over-cap requests are rejected (fail-fast, never silently dropped).
    """
    if group_by is None:
        return []
    keys = list(group_by)
    if len(keys) > MAX_COST_GROUP_BY_KEYS:
        raise ValueError(
            f"too many cost group-by keys ({len(keys)} > "
            f"{MAX_COST_GROUP_BY_KEYS})"
        )
    seen: set[str] = set()
    for key in keys:
        if not isinstance(key, str) or key not in SWS_SUPPORTED_COST_GROUP_BY_KEYS:
            raise ValueError(
                f"unsupported cost group-by key {key!r}; supported: "
                f"{sorted(SWS_SUPPORTED_COST_GROUP_BY_KEYS)}"
            )
        if key in seen:
            raise ValueError(f"duplicate cost group-by key {key!r}")
        seen.add(key)
    return keys


class CostExplorerCollector(_InventoryCollectorBase):
    """Collects account-level cost estimates through an injected client.

    The client must expose ``get_cost_and_usage(**params)`` returning a
    Cost Explorer shaped response: ``ResultsByTime`` where every entry either
    carries ``Total.UnblendedCost`` (ungrouped query) or ``Groups`` with
    ``Keys`` and ``Metrics.UnblendedCost`` (grouped query). Amounts are
    numeric strings, matching the real service.
    """

    def __init__(self, client: Any, *, trace: TraceSink | None = None):
        super().__init__(SWSResourceType.COST_DATA, trace=trace)
        self._client = client

    def collect(
        self,
        *,
        window_days: int | None = None,
        end_date: date | None = None,
        group_by: list[str] | None = None,
    ) -> list[CostEstimate]:
        """Collect cost estimates for an explicit, bounded window.

        ``end_date`` is required: it is the last (inclusive) day of the
        window and must be a ``datetime.date``. ``window_days`` counts the
        days ending on ``end_date`` and defaults to the canonical maximum.
        ``group_by`` requests per-key aggregates in addition to the account
        total; the total is always collected first.

        On a PRIMARY (total query) failure an empty list is returned after a
        FAILED trace event. Grouped queries that fail are traced as
        ENRICHMENT failures and the collector still returns the totals and
        any grouped aggregates that succeeded.
        """
        window = normalize_window_days(window_days)
        if end_date is None or not isinstance(end_date, date):
            raise ValueError("end_date is required and must be a datetime.date")
        keys = normalize_group_by(group_by)
        end = end_date.date() if isinstance(end_date, datetime) else end_date
        period = {
            "Start": (end - timedelta(days=window - 1)).isoformat(),
            "End": (end + timedelta(days=1)).isoformat(),
        }

        self._trace_start(
            f"collecting cost estimates for {period['Start']}..{period['End']}"
        )

        estimates: list[CostEstimate] = []
        total = self._query_total(period)
        if total is None:
            return []  # fail-closed when the primary total query failed
        estimates.extend(total)
        for key in keys:
            estimates.extend(self._query_grouped(period, key))
        estimates.sort(key=lambda row: row.line_item)
        return estimates

    # -- queries -------------------------------------------------------------

    def _query_total(self, period: dict[str, str]) -> list[CostEstimate] | None:
        """Run the ungrouped total query; None signals a PRIMARY failure."""
        try:
            response = self._client.get_cost_and_usage(
                TimePeriod=period,
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
            )
        except Exception as exc:
            self._trace_fail(
                f"cost collection (total) failed: {exc}",
                category=CollectionFailureCategory.PRIMARY,
            )
            return None
        total = 0.0
        credits_excluded = False
        for entry in response.get("ResultsByTime") or []:
            amount = self._amount(entry.get("Total"))
            if amount is None:
                self._trace_fail(
                    f"skipped Cost Explorer entry with an unparseable total",
                    category=CollectionFailureCategory.PARSE,
                )
                continue
            if amount < 0:
                credits_excluded = True
                continue
            total += amount
        estimate = CostEstimate(
            line_item="total",
            amount_usd=total,
            basis=_basis(period, group_by_key=None),
            projected=True,
            assumptions=[CREDIT_EXCLUSION_ASSUMPTION] if credits_excluded else [],
        )
        self._trace_succeed_estimates(1)
        return [estimate]

    def _query_grouped(self, period: dict[str, str], key: str) -> list[CostEstimate]:
        """Run one grouped query for a single canonical key.

        A failed grouped query is an ENRICHMENT failure: it never discards
        the totals and never aborts the remaining grouped queries.
        """
        try:
            response = self._client.get_cost_and_usage(
                TimePeriod=period,
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
                GroupBy=[_COST_GROUP_BY_PARAMS[key]],
            )
        except Exception as exc:
            self._trace_fail(
                f"cost collection grouped by '{key}' failed: {exc}",
                category=CollectionFailureCategory.ENRICHMENT,
            )
            return []
        by_value: dict[str, float] = {}
        credits_excluded = False
        for entry in response.get("ResultsByTime") or []:
            for group in entry.get("Groups") or []:
                keys = group.get("Keys") or []
                if not keys:
                    self._trace_fail(
                        f"skipped Cost Explorer group with no keys (grouped by "
                        f"'{key}')",
                        category=CollectionFailureCategory.PARSE,
                    )
                    continue
                amount = self._amount(group.get("Metrics"))
                if amount is None:
                    self._trace_fail(
                        f"skipped Cost Explorer group with an unparseable amount "
                        f"(grouped by '{key}')",
                        category=CollectionFailureCategory.PARSE,
                    )
                    continue
                if amount < 0:
                    credits_excluded = True
                    continue
                value = keys[0]
                by_value[value] = by_value.get(value, 0.0) + amount
        estimates = [
            CostEstimate(
                line_item=f"{key}:{value}",
                amount_usd=amount,
                basis=_basis(period, group_by_key=key),
                projected=True,
                assumptions=[CREDIT_EXCLUSION_ASSUMPTION] if credits_excluded else [],
            )
            for value, amount in sorted(by_value.items())
        ]
        self._trace_succeed_estimates(len(estimates), group=key)
        return estimates

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _amount(metrics: Any) -> float | None:
        """Parse the ``UnblendedCost`` amount string, or None if unusable."""
        if not isinstance(metrics, dict):
            return None
        unblended = metrics.get("UnblendedCost")
        if not isinstance(unblended, dict):
            return None
        raw = unblended.get("Amount")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _trace_succeed_estimates(
        self, count: int, *, group: str | None = None
    ) -> None:
        """Record a SUCCEEDED event with a cost-appropriate message."""
        if self._trace is not None:
            metadata: dict[str, Any] = {
                "resource_type": self._resource_type.value,
                "count": count,
            }
            if group is not None:
                metadata["group_by"] = group
            self._trace.succeed(
                TraceEventType.INVENTORY_QUERY,
                f"collected {count} cost estimates",
                metadata=metadata,
            )


def _basis(period: dict[str, str], *, group_by_key: str | None) -> str:
    """Stable basis string describing the exact query that produced a row."""
    if group_by_key is None:
        return (
            f"cost_explorer:unblended_cost:total:"
            f"{period['Start']}:{period['End']}"
        )
    return (
        f"cost_explorer:unblended_cost:{group_by_key}:"
        f"{period['Start']}:{period['End']}"
    )