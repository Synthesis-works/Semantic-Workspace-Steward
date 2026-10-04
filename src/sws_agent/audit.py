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
  - Process-safe appends (M14-A). ``O_APPEND`` is **not** atomic on Windows:
    every writer seeks to the same end-of-file and the later write displaces
    the earlier one. The M13 Phase 2 investigation measured 169 of 720
    records silently lost across four processes with every child exiting
    ``0`` -- no malformed lines, no duplicates, no error anywhere. Appending
    therefore happens under an exclusive byte-range lock, and both the
    duplicate check and ``records()`` re-read through that lock so a process
    sees records written by its siblings rather than answering from a
    process-local cache. This makes the store safe as *evidence*; it does not
    make it an execution authority (ADR 0003), and nothing here may be relied
    on to decide "may I proceed?".
  - Sanitization. Envelopes and payloads never contain ``ResourceRecord.raw``,
    chain-of-thought/reasoning prose (the ``EXPLANATION`` record carries the
    provider, claim kind, and stable ``reason`` code only), credentials,
    secrets, raw AWS wire payloads, or raw Cost Explorer pages.
  - Fail-loud. ``AuditStoreError`` (and subclasses) propagate out of the
    backend so a persistence failure surfaces as a ``ToolError`` (via the
    ``_guarded`` seam) instead of a silently missed durable claim. ``close()``
    makes any later write fail fast. If a tool call itself fails, no audit
    write happens at all (no false durable claim is recorded).
  - The ``execution`` envelope stanza is reserved and always ``None``: M9
    deliberately makes NO schema change (no ``schema_version`` bump) and
    records execution transactions through the standard ``kind`` / ``payload``
    structure using ``AuditRecordKind.EXECUTION`` with the sanitized stage
    payload from ``execution_payload``.

The ledger is pure-python (only the standard library) so it remains hermetic
in the no-AWS, no-network unit-test suite.
"""

from __future__ import annotations

import enum
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from .constants import ExecutionOutcome, ExecutionStage, RefusalReason
from .models import (
    ActionPlan,
    ApprovalTicket,
    CollectionFailure,
    CostCollectionReport,
    CostEstimate,
    ExecutionRequest,
    MutationAttempt,
    PolicyDecision,
    ResourceRecord,
    VerificationResult,
    WorkspaceSnapshot,
)

AUDIT_SCHEMA_VERSION: int = 1
"""Version of the envelope schema this module writes (bumped on breaking change)."""

LEDGER_FILENAME: str = "audit.jsonl"
"""Canonical file name for a JSONL audit ledger within its directory."""

AUDIT_LOCK_TIMEOUT_SECONDS: float = 30.0
"""How long a writer waits for the append lock before failing loudly."""

AUDIT_LOCK_RETRY_SECONDS: float = 0.001
"""Delay between non-blocking lock attempts while ``AUDIT_LOCK_TIMEOUT_SECONDS`` runs."""

AUDIT_LOCK_REGION_BYTES: int = 1
"""Size of the advisory lock region held at offset 0 of the ledger.

One byte is enough: the region is never written to, it exists only so the
locking primitive has a target. It sits at offset 0 rather than at end-of-file
so the region is stable no matter how large the ledger grows.
"""


def _acquire_ledger_lock(fd: int) -> None:
    """Take the ledger's exclusive append lock.

    Windows uses ``msvcrt.locking`` and POSIX uses ``fcntl.flock``; both are
    advisory, which is sufficient because every writer of an SWS ledger goes
    through this module. Windows retries non-blockingly against an explicit
    deadline so a contended or deadlocked writer produces a named, actionable
    error instead of an opaque one from the platform's fixed retry policy.
    """
    if os.name == "nt":
        import msvcrt

        deadline = time.monotonic() + AUDIT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, AUDIT_LOCK_REGION_BYTES)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise AuditStoreError(
                        f"could not acquire the audit ledger lock on fd "
                        f"{fd} within {AUDIT_LOCK_TIMEOUT_SECONDS}s: {exc}"
                    ) from exc
                time.sleep(AUDIT_LOCK_RETRY_SECONDS)

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)


def _release_ledger_lock(fd: int) -> None:
    """Release the lock taken by :func:`_acquire_ledger_lock`."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, AUDIT_LOCK_REGION_BYTES)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _ledger_lock(fd: int) -> Iterator[None]:
    """Serialize a read-modify-append against the ledger.

    The whole sequence must happen inside one lock acquisition. Refreshing and
    appending separately would reintroduce the exact interleaving that loses
    records: a duplicate check performed outside the lock is a check against
    the future, not against what is on disk.
    """
    _acquire_ledger_lock(fd)
    try:
        yield
    finally:
        _release_ledger_lock(fd)


class AuditRecordKind(str, enum.Enum):
    """Stable record kinds written to the durable ledger."""

    RUN = "run"
    SNAPSHOT = "snapshot"
    DECISION = "decision"
    PLAN = "plan"
    TICKET = "ticket"
    EXPLANATION = "explanation"
    COST = "cost"
    EXECUTION = "execution"


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
    ``execution`` is reserved and always ``None`` (M9 records execution
    transactions through ``kind`` + sanitized ``payload`` instead).
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

    Every append and every read happens under the ledger's exclusive lock, and
    the in-process view is rebuilt from disk inside that lock. A second process
    (or a second handle in this process) therefore observes the first one's
    records instead of silently losing them -- see the module docstring for the
    measurement that forced this.
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
        # Binary append: no text-encoding layer may re-buffer or re-seek this
        # write. The append position is established by an explicit end-of-file
        # seek inside the lock, not by whatever offset the handle last held.
        self._file = self._path.open("a+b")
        with _ledger_lock(self._file.fileno()):
            self._refresh()

    def _refresh(self) -> None:
        """Rebuild the in-process view from the ledger on disk.

        Must be called while holding :func:`_ledger_lock`, which is what makes
        the result a consistent snapshot rather than a torn read. Replaces the
        cache wholesale so records deleted or corrupted by another process are
        reflected here too, instead of lingering as a stale local belief.
        """
        try:
            self._file.seek(0)
            raw = self._file.read().decode("utf-8")
        except OSError as exc:
            raise AuditStoreError(
                f"could not read existing audit ledger {self._path}: {exc}"
            ) from exc
        records: dict[str, AuditEnvelope] = {}
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
            records[envelope.record_id] = envelope
        self._records = records

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
        line = (envelope.model_dump_json() + "\n").encode("utf-8")
        with _ledger_lock(self._file.fileno()):
            # Refreshed before the duplicate check so a colliding id written by
            # a sibling process is caught. Checking the cached dict instead
            # would accept an id that is already on disk.
            self._refresh()
            if record_id in self._records:
                raise DuplicateRecordError(
                    f"audit record id {record_id!r} already exists in {self._path}"
                )
            try:
                self._file.seek(0, os.SEEK_END)
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
        """Every envelope currently on disk, ordered by ``record_id``.

        Reads through the lock and rebuilds the cache, so this is a
        cross-process view. Returning the cached list instead would report
        only what *this* process wrote, which is the blind spot ADR 0003
        measured.
        """
        with _ledger_lock(self._file.fileno()):
            self._refresh()
            return [
                self._records[record_id]
                for record_id in sorted(self._records)
            ]

    def __len__(self) -> int:
        if self._closed:
            return len(self._records)
        with _ledger_lock(self._file.fileno()):
            self._refresh()
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


def execution_payload(
    *,
    stage: ExecutionStage,
    execution_id: str | None = None,
    request: ExecutionRequest | None = None,
    outcome: ExecutionOutcome | None = None,
    refusal: RefusalReason | None = None,
    refusal_detail: str = "",
    attempt: MutationAttempt | None = None,
    verification: VerificationResult | None = None,
    consumed: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """Sanitized execution-stage payload (M9).

    Written through the standard EXECUTION kind + payload structure; the
    reserved ``execution`` envelope stanza deliberately stays ``None`` (no
    schema change in M9). The payload carries only identifiers, canonical
    enum values, and plain verification facts (``expected``/``observed``
    values are sanitized domain facts, never raw AWS wire payloads,
    credentials, or secrets). A mutation-call error is recorded as
    ``attempt.call_error`` / ``attempt.ambiguous`` flags plus a sanitized
    note, never an exception traceback.
    """
    payload: dict[str, Any] = {
        "stage": stage.value,
        "consumed": consumed,
        "note": note,
    }
    if execution_id is not None:
        payload["execution_id"] = execution_id
    if request is not None:
        payload["resource_id"] = request.resource_id
        payload["action"] = request.action.value
        payload["execution_mode"] = request.execution_mode.value
    if outcome is not None:
        payload["outcome"] = outcome.value
    if refusal is not None:
        payload["refusal"] = refusal.value
        if refusal_detail:
            payload["refusal_detail"] = refusal_detail
    if attempt is not None:
        payload["attempt"] = attempt.model_dump(mode="json")
    if verification is not None:
        payload["verification"] = {
            "status": verification.status.value,
            "expected_facts": dict(verification.expected_facts),
            "observed_facts": dict(verification.observed_facts),
            "details": list(verification.details),
            "observed_at": (
                verification.observed_at.isoformat()
                if verification.observed_at is not None
                else None
            ),
        }
    return payload