"""Workspace-level snapshot collection (M2C-B).

Fresh SWS module (no SMS reuse). Runs the M2B collectors through a single
hermetic collection pass and derives a deterministic ``WorkspaceSnapshot``
from the run-scoped trace events plus canonicalized resources.

Approved M2C-B decisions implemented here:
- Caller-provided ``TraceRecorder`` is used when supplied; otherwise a fresh
  recorder is created for the run.
- Only events appended during this run are analyzed (run-scoping by start
  length), so a shared recorder's prior history never leaks into the snapshot.
- ``regions`` is required and must be non-empty (ValueError otherwise); the
  snapshot is never implicitly global.
- ``truncated`` is derived only from collector-emitted ``truncated: True``
  metadata with genuine evidence of more data; it is never inferred from
  ``count == limit``.
- ``collected_at`` is the timestamp of the last run-scoped INVENTORY_QUERY
  event, or None when the run recorded no such event (confirmed absence is
  never invented).
- ``resource_types`` records intent: every registered collector type is
  listed even if its primary collection failed.
- ``failures`` are mapped 1:1 from FAILED INVENTORY_QUERY events, carrying
  the collector's ``category`` (defensive fallback when absent: SUCCEEDED
  present for the type means ENRICHMENT, else PRIMARY).
- ``partial`` is True whenever any run-scoped INVENTORY_QUERY FAILED event
  exists, whether the failure is fatal (a primary list operation failed) or
  non-fatal (an enrichment lookup failed or a returned item was skipped).
- No LLM, AWS SDK, network, or destructive operations occur here.

M2C-E cost integration: when ``collect_cost`` is True, the run additionally
collects account-level cost estimates through ``CostExplorerCollector`` on
the same injected client and records them on ``snapshot.cost``. Cost data is
additive and isolated: it never enters ``resources`` or ``counts`` (it is
account-level aggregated data, not per-resource inventory), and its failures
flow through the same run-scoped INVENTORY_QUERY ``partial`` / ``failures``
contract. ``cost_end_date`` is required (and must be an explicit date) so a
cost run is deterministic with no current-time dependence; ``collect_cost``
defaults to False, preserving every existing caller.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from .canonical import canonicalize_resources
from .constants import (
    CollectionFailureCategory,
    SWSResourceType,
    TraceEventType,
    TraceStatus,
)
from .cost_explorer import CostExplorerCollector
from .inventory import INVENTORY_COLLECTORS, collect_all, normalize_limit
from .models import CollectionFailure, CostEstimate, ResourceRecord, WorkspaceSnapshot
from .trace import TraceRecorder


def _parse_trace_timestamp(value: str) -> datetime | None:
    """Parse a trace ISO timestamp to an aware UTC datetime, or None.

    Returns None when the value cannot be parsed or is naive (a naive
    timestamp is ambiguous and must not become an authoritative fact).
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _classify_failure_category(
    category_value: Any,
    *,
    resource_type: SWSResourceType,
    succeeded_types: set[SWSResourceType],
) -> CollectionFailureCategory:
    """Resolve a FAILED event's category, with the approved defensive fallback.

    Collectors always emit an explicit ``category``. When it is absent or
    invalid, a type that also produced a SUCCEEDED event during the run must
    have failed on enrichment (its primary list worked); otherwise the
    failure is classified as primary.
    """
    try:
        return CollectionFailureCategory(category_value)
    except (TypeError, ValueError):
        if resource_type in succeeded_types:
            return CollectionFailureCategory.ENRICHMENT
        return CollectionFailureCategory.PRIMARY


def _failure_from_event(
    event: Any,
    *,
    succeeded_types: set[SWSResourceType],
) -> CollectionFailure | None:
    """Map a run-scoped FAILED INVENTORY_QUERY event to a CollectionFailure.

    Events that cannot be attributed to a known resource type are skipped
    (they are not records produced by SWS collectors), so no type is ever
    fabricated. ``trace_event_id`` links the failure 1:1 to the source event.
    """
    metadata = dict(event.metadata or {})
    try:
        resource_type = SWSResourceType(metadata.get("resource_type"))
    except (TypeError, ValueError):
        return None
    category = _classify_failure_category(
        metadata.get("category"),
        resource_type=resource_type,
        succeeded_types=succeeded_types,
    )
    return CollectionFailure(
        resource_type=resource_type,
        source=resource_type.value,
        category=category,
        message=event.message,
        fatal=category is CollectionFailureCategory.PRIMARY,
        resource_id=metadata.get("resource_id"),
        trace_event_id=event.event_id,
    )


def collect_workspace(
    *,
    client: Any,
    regions: list[str] | None = None,
    limit: int | None = None,
    trace: TraceRecorder | None = None,
    snapshot_id: str | None = None,
    run_id: str | None = None,
    partition: str = "aws",
    now: datetime | None = None,
    collect_cost: bool = False,
    cost_window_days: int | None = None,
    cost_group_by: list[str] | None = None,
    cost_end_date: date | None = None,
) -> WorkspaceSnapshot:
    """Collect a deterministic snapshot of a workspace's inventory.

    Runs every registered collector through ``collect_all`` and derives the
    snapshot's outcome fields from the run-scoped trace events. ``client``
    supplies the injected AWS-like client (the collector protocol). No live
    AWS access occurs in this milestone.

    ``regions`` is required: S3 buckets are discovered globally by the S3
    collector, but Lambda inventory is region-driven, so an empty regions
    list would silently collect an incomplete workspace. An empty or invalid
    list raises ValueError before any collector runs.

    ``trace`` is reused when provided; otherwise a fresh recorder is created
    for the run. ``snapshot_id`` defaults to a fresh hex UUID when omitted.
    ``now`` must be timezone-aware when supplied; otherwise the current UTC
    time is used.

    Cost collection (M2C-E): passing ``collect_cost=True`` requires
    ``cost_end_date`` (a ``datetime.date``), otherwise ValueError. The
    collector validates ``cost_window_days`` and ``cost_group_by`` and raises
    ValueError for out-of-bounds or unknown values. The injected ``client``
    must expose ``get_cost_and_usage`` when cost collection is requested.
    """
    if not regions:
        raise ValueError(
            "at least one AWS region is required to collect workspace inventory"
        )
    for region in regions:
        if not isinstance(region, str) or not region.strip():
            raise ValueError("workspace regions must be non-empty strings")

    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    if collect_cost and cost_end_date is None:
        raise ValueError(
            "cost_end_date is required when collect_cost is True"
        )

    effective_limit = normalize_limit(limit)
    recorder = trace if trace is not None else TraceRecorder()
    start_len = len(recorder)
    created_at = (
        now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    )

    collections = collect_all(
        client=client,
        limit=effective_limit,
        trace=recorder,
        regions=list(regions),
    )

    cost_estimates: list[CostEstimate] = []
    if collect_cost:
        cost_estimates = CostExplorerCollector(client, trace=recorder).collect(
            window_days=cost_window_days,
            end_date=cost_end_date,
            group_by=cost_group_by,
        )

    run_events = list(recorder)[start_len:]
    inventory_events = [
        event
        for event in run_events
        if event.event_type is TraceEventType.INVENTORY_QUERY
    ]

    succeeded_types: set[SWSResourceType] = set()
    valid_type_values = {resource_type.value for resource_type in SWSResourceType}
    for event in inventory_events:
        if event.status is not TraceStatus.SUCCEEDED:
            continue
        type_value = (event.metadata or {}).get("resource_type")
        if type_value in valid_type_values:
            succeeded_types.add(SWSResourceType(type_value))

    truncated = any(
        event.status is TraceStatus.SUCCEEDED
        and (event.metadata or {}).get("truncated") is True
        for event in inventory_events
    )

    failures = [
        failure
        for event in inventory_events
        if event.status is TraceStatus.FAILED
        for failure in [_failure_from_event(event, succeeded_types=succeeded_types)]
        if failure is not None
    ]

    collected_at = (
        _parse_trace_timestamp(inventory_events[-1].timestamp)
        if inventory_events
        else None
    )

    resource_types = list(INVENTORY_COLLECTORS)
    resources = canonicalize_resources(
        (
            record
            for resource_type in resource_types
            for record in collections.get(resource_type, [])
        ),
        partition=partition,
    )
    counts = {
        resource_type: sum(1 for record in resources if record.resource_type is resource_type)
        for resource_type in resource_types
    }

    return WorkspaceSnapshot(
        snapshot_id=snapshot_id if snapshot_id else uuid4().hex,
        created_at=created_at,
        collected_at=collected_at,
        run_id=run_id,
        requested_limit=effective_limit,
        regions=list(regions),
        resource_types=resource_types,
        resources=resources,
        counts=counts,
        truncated=truncated,
        partial=any(
            event.status is TraceStatus.FAILED for event in inventory_events
        ),
        failures=failures,
        cost=cost_estimates,
    )