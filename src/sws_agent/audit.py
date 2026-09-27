"""Append-only durable audit ledger for SWS tool calls (M8).

M8 introduces persistence behind the existing read-only tools: the ledger
records provenance (``RUN``), the collected snapshot (``SNAPSHOT``), policy
decisions (``DECISION``), authorization plans and tickets (``PLAN``,
``TICKET``), cost collection (``COST``), and explanation events
(``EXPLANATION``), all keyed by the M8 lineage identifiers threaded through
the domain models.

Guarantees and boundaries:

  - Append-only JSONL is authoritative. Lines are UTF-8, one envelope per
    line, written with flush + fsync so a crash does not silently lose a
    just-acknowledged write. A duplicate ``record_id`` (id-source collision
    or re-open corruption) raises ``DuplicateRecordError``; a malformed
    existing line fails fast with ``CorruptLedgerError`` rather than being
    skipped. There is no rotation and no deletion path in M8: audit history
    never silently disappears (the boundary is documented; a size guard would
    require operator decision and is explicitly out of scope here).
  - Sanitization. Envelopes and payloads never contain ``ResourceRecord.raw``,
    chain-of-thought/reasoning prose (the ``EXPLANATION`` record carries the
    provider, claim kind, and stable ``reason`` code only), credentials,
    secrets, raw AWS wire payloads, or raw Cost Explorer pages.
  - Fail-loud. ``AuditStoreError`` (and subclasses) propagate out of the
    backend so a persistence failure surfaces as a ``ToolError`` (via the
    ``_guarded`` seam) instead of a silently missed durable claim. ``close()``
    makes any later write fail fast. If a tool call itself fails, no audit
    write happens at all (no false durable claim is recorded).
  - The ``execution`` stanza is reserved and always ``None`` in M8; M9 will
    own it once an executor exists.

The ledger is pure-python (only the standard library) so it remains hermetic
in the no-AWS, no-network unit-test suite.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from .models import (
    ActionPlan,
    ApprovalTicket,
    CollectionFailure,
    CostCollectionReport,
    CostEstimate,
    PolicyDecision,
    ResourceRecord,
    WorkspaceSnapshot,
)

AUDIT_SCHEMA_VERSION: int = 1
"""Version of the envelope schema this module writes (bumped on breaking change)."""

LEDGER_FILENAME: str = "audit.jsonl"
"""Canonical file name for a JSONL audit ledger within its directory."""


class AuditRecordKind(str, enum.Enum):
    """Stable record kinds written to the durable ledger."""

    RUN = "run"
    SNAPSHOT = "snapshot"
    DECISION = "decision"
    PLAN = "plan"
    TICKET = "ticket"
    EXPLANATION = "explanation"
    COST = "cost"


class AuditStoreError(RuntimeError):
    """Base class for durable-ledger persistence failures (fail-loud)."""


class DuplicateRecordError(AuditStoreError):
    """A write collided with an existing ``record_id`` in the ledger."""


class CorruptLedgerError(AuditStoreError):
    """An existing ledger line could not be parsed as an envelope."""


class ClosedAuditStoreError(AuditStoreError):
    """A write was attempted on a store that has been closed."""


class AuditEnvelope(BaseModel):
    """One durable ledger record.

    ``record_id`` and ``created_at`` are stamped by the store at write time
    (never trusted from callers), so the ledger owns identity and time.
    ``execution`` is reserved for M9 and is always ``None`` here.
    """

    schema_version: int = Field(default=AUDIT_SCHEMA_VERSION, ge=1)
    record_id: str | None = Field(default=None, min_length=1)
    kind: AuditRecordKind
    run_id: str | None = Field(default=None, min_length=1)
    snapshot_id: str | None = Field(default=None, min_length=1)
    decision_id: str | None = Field(default=None, min_length=1)
    action_plan_id: str | None = Field(default=None, min_length=1)
    ticket_id: str | None = Field(default=None, min_length=1)
    resource_id: str | None = Field(default=None, min_length=1)
    created_at: datetime | None = None
    execution: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("created_at", mode="before")
    @classmethod
    def _require_aware_datetime(cls, value: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("audit records must carry timezone-aware timestamps")
        return value


@runtime_checkable
class AuditStore(Protocol):
    """Typed durable-write seam used by the MCP backend."""

    def write(
        self,
        kind: AuditRecordKind,
        *,
        run_id: str | None = None,
        snapshot_id: str | None = None,
        decision_id: str | None = None,
        action_plan_id: str | None = None,
        ticket_id: str | None = None,
        resource_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEnvelope: ...

    def records(self) -> list[AuditEnvelope]: ...

    def close(self) -> None: ...

    def __len__(self) -> int: ...


class JsonlAuditStore:
    """Append-only JSONL audit store (UTF-8, flush + fsync per write).

    On construction the target parent directory is created (fail-fast on
    unusable paths) and any existing ledger is parsed so ``record_id``
    collisions are detected from the very first write. ``now`` must return
    timezone-aware datetimes; ``id_source`` produces ``record_id`` values.
    Both are injectable for deterministic tests.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
        id_source: Callable[[], str] | None = None,
    ) -> None:
        self._path = Path(path)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._id_source = id_source or (lambda: uuid4().hex)
        # Fail fast at construction on a naive clock, before any write.
        if self._now().tzinfo is None:
            raise ValueError(
                "audit store clock must return timezone-aware datetimes"
            )
        self._records: dict[str, AuditEnvelope] = {}
        self._closed = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8", newline="\n")
        self._load_existing()

    def _load_existing(self) -> None:
        """Parse the current ledger so re-opens rebuild lineage + detect dupes."""
        if self._path.exists():
            try:
                raw = self._path.read_text(encoding="utf-8")
            except OSError as exc:
                raise AuditStoreError(
                    f"could not read existing audit ledger {self._path}: {exc}"
                ) from exc
            if raw:
                for line_number, line in enumerate(raw.splitlines(), start=1):
                    if not line.strip():
                        continue
                    try:
                        envelope = AuditEnvelope.model_validate_json(line)
                    except Exception as exc:  # noqa: BLE001 - fail-fast boundary
                        raise CorruptLedgerError(
                            f"audit ledger {self._path} line {line_number} is "
                            f"not a valid envelope: {exc}"
                        ) from exc
                    if envelope.record_id is None:
                        raise CorruptLedgerError(
                            f"audit ledger {self._path} line {line_number} "
                            "has no record_id"
                        )
                    self._records[envelope.record_id] = envelope

    def write(
        self,
        kind: AuditRecordKind,
        *,
        run_id: str | None = None,
        snapshot_id: str | None = None,
        decision_id: str | None = None,
        action_plan_id: str | None = None,
        ticket_id: str | None = None,
        resource_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEnvelope:
        if self._closed:
            raise ClosedAuditStoreError(
                f"audit store {self._path} is closed; cannot write"
            )
        record_id = self._id_source()
        if not record_id:
            raise AuditStoreError(
                f"audit id source returned an empty id for {self._path}"
            )
        if record_id in self._records:
            raise DuplicateRecordError(
                f"audit record id {record_id!r} already exists in {self._path}"
            )
        envelope = AuditEnvelope(
            record_id=record_id,
            kind=kind,
            run_id=run_id,
            snapshot_id=snapshot_id,
            decision_id=decision_id,
            action_plan_id=action_plan_id,
            ticket_id=ticket_id,
            resource_id=resource_id,
            created_at=self._now(),
            payload=payload or {},
        )
        line = envelope.model_dump_json() + "\n"
        try:
            self._file.write(line)
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError as exc:
            raise AuditStoreError(
                f"could not append audit record to {self._path}: {exc}"
            ) from exc
        self._records[record_id] = envelope
        return envelope

    def records(self) -> list[AuditEnvelope]:
        return [
            self._records[record_id]
            for record_id in sorted(self._records)
        ]

    def __len__(self) -> int:
        return len(self._records)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._file.flush()
            self._file.close()
        except OSError as exc:
            raise AuditStoreError(
                f"could not close audit ledger {self._path}: {exc}"
            ) from exc

    @property
    def path(self) -> Path:
        return self._path


# --- Sanitized payload builders ------------------------------------------
# Every builder returns plain JSON-safe dicts (via pydantic ``mode="json"``)
# and explicitly excludes fields that must never enter the ledger.


def _resource_payload(record: ResourceRecord) -> dict[str, Any]:
    """Sanitized resource record: everything except ``raw`` (forbidden)."""
    payload = record.model_dump(mode="json")
    payload.pop("raw", None)
    return payload


def _estimate_payload(estimate: CostEstimate) -> dict[str, Any]:
    return estimate.model_dump(mode="json")


def _failure_payload(failure: CollectionFailure) -> dict[str, Any]:
    return failure.model_dump(mode="json")


def snapshot_payload(snapshot: WorkspaceSnapshot) -> dict[str, Any]:
    """Snapshot intent, completeness, failures, cost, and sanitized resources."""
    return {
        "snapshot_id": snapshot.snapshot_id,
        "run_id": snapshot.run_id,
        "requested_limit": snapshot.requested_limit,
        "regions": list(snapshot.regions),
        "resource_types": [t.value for t in snapshot.resource_types],
        "created_at": snapshot.created_at.isoformat(),
        "collected_at": (
            snapshot.collected_at.isoformat()
            if snapshot.collected_at is not None
            else None
        ),
        "counts": {t.value: n for t, n in snapshot.counts.items()},
        "truncated": snapshot.truncated,
        "partial": snapshot.partial,
        "failures": [
            _failure_payload(failure) for failure in snapshot.failures
        ],
        "cost": [_estimate_payload(estimate) for estimate in snapshot.cost],
        "resources": [
            _resource_payload(record) for record in snapshot.resources
        ],
    }


def run_payload(run_type: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Provenance payload: the run type and its ordered trace events."""
    return {"run_type": run_type, "events": list(events)}


def decision_payload(decision: PolicyDecision) -> dict[str, Any]:
    return decision.model_dump(mode="json")


def plan_payload(plan: ActionPlan) -> dict[str, Any]:
    return plan.model_dump(mode="json")


def ticket_payload(ticket: ApprovalTicket) -> dict[str, Any]:
    return ticket.model_dump(mode="json")


def explanation_payload(
    resource_id: str,
    decision_id: str,
    *,
    snapshot_id: str | None,
    run_id: str | None,
    provider: str,
    claim_kind: str,
    reason: str,
) -> dict[str, Any]:
    """Explanation provenance: never the prose ``text`` or any reasoning text."""
    return {
        "resource_id": resource_id,
        "decision_id": decision_id,
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "provider": provider,
        "claim_kind": claim_kind,
        "reason": reason,
    }


def cost_payload(
    *,
    end_date: str,
    window_days: int | None,
    group_by: list[str] | None,
    report: CostCollectionReport,
) -> dict[str, Any]:
    """Request intent plus the honest completeness summary (no raw pages)."""
    return {
        "end_date": end_date,
        "window_days": window_days,
        "group_by": list(group_by) if group_by else [],
        "estimates": [
            _estimate_payload(estimate) for estimate in report.estimates
        ],
        "truncated": report.truncated,
        "failures": [
            _failure_payload(failure) for failure in report.failures
        ],
    }