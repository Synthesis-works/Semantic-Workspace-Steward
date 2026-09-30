"""Gated execution boundary and the guarded mutation registry (M9).

M9 builds the execution-safety machinery while deliberately keeping the
production system unable to mutate AWS:

  - ``ACTION_EXECUTION_REGISTRY`` is metadata only. Every entry carries a
    documentation-only ``mutation`` string (for example
    ``"ec2:StopInstances"``) and ``implemented=False``. The registry holds no
    callables, no AWS clients, and no imports of the AWS SDK; it only
    describes what a future, separately-reviewed milestone could register.
  - ``ExecutionCoordinator`` runs the deterministic gate (A1 identity,
    A2 authorization/approval, A3 decision consistency, A4 idempotency,
    A5 freshness) and cross-checks the plan, the decision that motivated it,
    the snapshot it was derived from, the GRANTED approval ticket, and a
    fresh post-snapshot observation. A refused request performs NO AWS call
    and writes NO audit claim.
  - The mutation boundary itself is the injected ``MutationHandler``
    protocol. M9 registers no handler: in production every gate-passing
    request ends ``NOT_EXECUTED`` with the honest note that nothing was
    executed. Tests inject deterministic fakes to exercise the full
    PRE -> ATTEMPT -> RESULT -> POST contract.

Boundaries: this module never imports the AWS SDK, never constructs an AWS
API client, never calls arbitrary AWS methods, and never enables
autonomous execution of approval-required actions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Mapping, Protocol, runtime_checkable
from uuid import uuid4

from ._identity import utc_now
from .approval import InMemoryApprovalStore, UnknownTicketError
from .audit import AuditRecordKind, AuditStore, execution_payload
from .constants import (
    ApprovalStatus,
    ExecutionMode,
    ExecutionOutcome,
    ExecutionStage,
    PotentialAction,
    RefusalReason,
    SWSResourceType,
    VerificationStatus,
)
from .models import (
    ExecutionRequest,
    ExecutionResult,
    MutationAttempt,
    ResourceObservation,
    VerificationResult,
    WorkspaceSnapshot,
)
from .verification import (
    DefaultOutcomeVerifier,
    ObservationError,
    ObservationProvider,
    OutcomeVerifier,
)


@dataclass(frozen=True)
class ActionSpec:
    """Static description of one action's execution contract.

    ``mutation`` is documentation only (a plain string naming the future
    AWS operation, for example ``"ec2:StopInstances"``). It is never a
    callable and never a live client reference; ``implemented=False`` in M9
    means no code exists anywhere that could perform this mutation.
    """

    action: PotentialAction
    eligible_resource_types: frozenset[SWSResourceType]
    requires_human_approval: bool
    mutation: str | None
    implemented: bool = False
    description: str = ""


_ACTION_EXECUTION_REGISTRY_SOURCE: dict[PotentialAction, ActionSpec] = {
    PotentialAction.LEAVE: ActionSpec(
        action=PotentialAction.LEAVE,
        eligible_resource_types=frozenset(),
        requires_human_approval=False,
        mutation=None,
        implemented=False,
        description=(
            "LEAVE is a no-op by definition and has no execution boundary "
            "to cross."
        ),
    ),
    PotentialAction.FLAG_FOR_REVIEW: ActionSpec(
        action=PotentialAction.FLAG_FOR_REVIEW,
        eligible_resource_types=frozenset(),
        requires_human_approval=False,
        mutation=None,
        implemented=False,
        description=(
            "FLAG_FOR_REVIEW surfaces a resource for human attention; it "
            "has no execution boundary to cross."
        ),
    ),
    PotentialAction.REQUEST_APPROVAL: ActionSpec(
        action=PotentialAction.REQUEST_APPROVAL,
        eligible_resource_types=frozenset(),
        requires_human_approval=False,
        mutation=None,
        implemented=False,
        description=(
            "REQUEST_APPROVAL opens the human approval flow; there is "
            "nothing to execute."
        ),
    ),
    PotentialAction.STOP_RESOURCE: ActionSpec(
        action=PotentialAction.STOP_RESOURCE,
        eligible_resource_types=frozenset({SWSResourceType.EC2_INSTANCE}),
        requires_human_approval=True,
        mutation=(
            "ec2:StopInstances (documented future boundary only; no "
            "mutation handler is registered in M9)"
        ),
        implemented=False,
        description=(
            "STOP_RESOURCE targets EC2 instances and requires human "
            "approval, decision/snapshot consistency, a fresh observation, "
            "and a registered mutation handler. M9 registers no handler."
        ),
    ),
}

ACTION_EXECUTION_REGISTRY: Final[Mapping[PotentialAction, ActionSpec]] = (
    MappingProxyType(_ACTION_EXECUTION_REGISTRY_SOURCE)
)
"""Immutable registry: metadata only, no callables, nothing executable."""


@runtime_checkable
class MutationHandler(Protocol):
    """The single seam where a mutation could occur.

    Production registers no handler in M9. A handler receives the fully
    gated ``ExecutionRequest`` and returns an opaque ``MutationAttempt``;
    it must never raise to report a normal outcome (an exception is treated
    as an ambiguous, unknown attempt).
    """

    def handle(self, request: ExecutionRequest) -> MutationAttempt: ...


class ExecutionCoordinator:
    """Deterministic gate keeper for one execution attempt (M9).

    The coordinator is read-only over the plan, decision, snapshot, and
    ticket it is handed; it never mutates AWS and never mutates those
    objects. It owns the execution attempt's idempotency (in-memory plus,
    when an ``AuditStore`` is injected, a durable duplicate scan over prior
    EXECUTION records) and, on gated execution, consumes the granted ticket
    so it can never authorize a second attempt.

    Refusals write no audit record: M9 never records a false durable claim,
    and a refused request performs no AWS call.
    """

    def __init__(
        self,
        *,
        approval_store: InMemoryApprovalStore,
        handler: MutationHandler | None = None,
        observer: ObservationProvider | None = None,
        verifier: OutcomeVerifier | None = None,
        audit_store: AuditStore | None = None,
        id_source: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = approval_store
        self._handler = handler
        self._observer = observer
        self._verifier = (
            verifier if verifier is not None else DefaultOutcomeVerifier()
        )
        self._audit_store = audit_store
        self._id_source = id_source or (lambda: uuid4().hex)
        self._now = now or utc_now
        self._attempted: set[str] = set()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Run the gate and, if it passes, cross the boundary exactly once."""
        started_at = self._now()
        execution_id = self._id_source()
        gate = self._gate(request)
        if gate is not None:
            reason, detail = gate
            return ExecutionResult(
                execution_id=execution_id,
                action_plan_id=request.action_plan_id,
                resource_id=request.resource_id,
                action=request.action,
                execution_mode=request.execution_mode,
                outcome=ExecutionOutcome.REFUSED,
                refusal=reason,
                refusal_detail=detail,
                started_at=started_at,
                completed_at=self._now(),
            )

        spec = ACTION_EXECUTION_REGISTRY[request.action]
        self._write(
            stage=ExecutionStage.PRE,
            request=request,
            execution_id=execution_id,
            note="gate passed; intent recorded",
        )

        # M9 honesty boundary: no mutation handler is registered, so even a
        # fully gated request must report NOT_EXECUTED, never a fabricated
        # success.
        if self._handler is None:
            return ExecutionResult(
                execution_id=execution_id,
                action_plan_id=request.action_plan_id,
                resource_id=request.resource_id,
                action=request.action,
                execution_mode=request.execution_mode,
                outcome=ExecutionOutcome.NOT_EXECUTED,
                started_at=started_at,
                completed_at=self._now(),
                note=(
                    "no mutation handler is registered; the gate passed "
                    "but nothing was executed"
                ),
            )

        self._attempted.add(request.action_plan_id)
        attempt: MutationAttempt | None = None
        handler_note = ""
        try:
            attempt = self._handler.handle(request)
        except Exception as exc:  # noqa: BLE001 - boundary never fabricates success
            handler_note = (
                f"mutation handler raised {type(exc).__name__}; "
                "outcome cannot be established"
            )
        self._write(
            stage=ExecutionStage.ATTEMPT,
            request=request,
            execution_id=execution_id,
            attempt=attempt,
            note=(
                handler_note
                or (
                    "mutation boundary crossed"
                    if attempt is not None and not attempt.ambiguous
                    else "mutation boundary reached"
                )
            ),
        )

        verification_status: VerificationStatus
        verification_record: VerificationResult | None = None
        if attempt is not None and attempt.ambiguous:
            verification_status = VerificationStatus.UNKNOWN
            verification_record = VerificationResult(
                status=VerificationStatus.UNKNOWN,
                details=["attempt outcome is ambiguous; post-state cannot be verified"],
            )
        elif attempt is not None and attempt.call_error:
            verification_status = VerificationStatus.FAILED
            verification_record = VerificationResult(
                status=VerificationStatus.FAILED,
                details=["mutation call reported an error"],
            )
        elif handler_note:
            verification_status = VerificationStatus.UNKNOWN
            verification_record = VerificationResult(
                status=VerificationStatus.UNKNOWN,
                details=[handler_note],
            )
        elif self._observer is None:
            verification_status = VerificationStatus.UNKNOWN
            verification_record = VerificationResult(
                status=VerificationStatus.UNKNOWN,
                details=["no observation provider is configured"],
            )
        else:
            observation: ResourceObservation | None
            try:
                observation = self._observer.observe(request.resource_id)
            except ObservationError:
                observation = None
            if observation is None:
                verification_status = VerificationStatus.UNKNOWN
                verification_record = VerificationResult(
                    status=VerificationStatus.UNKNOWN,
                    details=["no observation available; post-state cannot be verified"],
                )
            elif observation.ambiguous:
                verification_status = VerificationStatus.UNKNOWN
                verification_record = VerificationResult(
                    status=VerificationStatus.UNKNOWN,
                    details=["observation is ambiguous; post-state cannot be verified"],
                )
            else:
                verification_record = self._verifier.verify(
                    observation=observation,
                    expected_facts=request.expected_poststate,
                )
                verification_status = verification_record.status
        self._write(
            stage=ExecutionStage.RESULT,
            request=request,
            execution_id=execution_id,
            attempt=attempt,
            verification=verification_record,
        )

        outcome = _outcome_for_status(verification_status)
        note_parts = [handler_note or ""] + list(
            verification_record.details if verification_record is not None else []
        )
        consumed = False
        if spec.requires_human_approval and request.ticket is not None:
            try:
                self._store.consume(request.ticket.ticket_id)
                consumed = True
            except Exception as exc:  # noqa: BLE001 - reported, never fabricated
                note_parts.append(
                    f"ticket {request.ticket.ticket_id} could not be "
                    f"consumed: {type(exc).__name__}"
                )
        self._write(
            stage=ExecutionStage.POST,
            request=request,
            execution_id=execution_id,
            outcome=outcome,
            verification=None,
            attempt=attempt,
            consumed=consumed,
            note="; ".join(part for part in note_parts if part),
        )
        return ExecutionResult(
            execution_id=execution_id,
            action_plan_id=request.action_plan_id,
            resource_id=request.resource_id,
            action=request.action,
            execution_mode=request.execution_mode,
            outcome=outcome,
            verification=verification_status,
            attempt_id=execution_id,
            started_at=started_at,
            completed_at=self._now(),
            note="; ".join(part for part in note_parts if part),
        )

    def _gate(
        self, request: ExecutionRequest
    ) -> tuple[RefusalReason, str] | None:
        plan = request.plan
        if plan is None:
            return RefusalReason.MISSING_PLAN, "request carries no plan"
        if plan.action_plan_id != request.action_plan_id:
            return (
                RefusalReason.MISSING_PLAN,
                "plan action_plan_id does not match the request",
            )
        if request.decision is None:
            return RefusalReason.MISSING_DECISION, "request carries no decision"
        if request.snapshot is None:
            return RefusalReason.MISSING_SNAPSHOT, "request carries no snapshot"
        if request.execution_mode is not plan.execution_mode:
            return (
                RefusalReason.MODE_MISMATCH,
                "request execution_mode does not match the plan execution_mode",
            )

        spec = ACTION_EXECUTION_REGISTRY.get(request.action)
        if spec is None or not spec.eligible_resource_types:
            return (
                RefusalReason.ACTION_NOT_EXECUTABLE,
                f"{request.action.value} has no eligible resource types",
            )

        resource = self._find_resource(request.snapshot, request.resource_id)
        if resource is None:
            return (
                RefusalReason.RESOURCE_NOT_IN_SNAPSHOT,
                "target resource is not present in the snapshot",
            )
        if resource.resource_type not in spec.eligible_resource_types:
            return (
                RefusalReason.RESOURCE_TYPE_MISMATCH,
                f"resource type {resource.resource_type.value} is not "
                f"eligible for {request.action.value}",
            )

        if request.snapshot.partial:
            return (
                RefusalReason.PARTIAL_SNAPSHOT,
                "snapshot is partial; target state cannot be trusted",
            )
        if request.snapshot.truncated:
            return (
                RefusalReason.TRUNCATED_SNAPSHOT,
                "snapshot is truncated; target state cannot be trusted",
            )

        if spec.requires_human_approval:
            if request.execution_mode is ExecutionMode.AUTONOMOUS:
                return (
                    RefusalReason.AUTONOMOUS_EXECUTION_UNSUPPORTED,
                    "M9 does not enable autonomous execution of "
                    "approval-required actions",
                )
            gate = self._ticket_gate(request)
            if gate is not None:
                return gate

        gate = self._decision_gate(request)
        if gate is not None:
            return gate

        if plan.executed:
            return (
                RefusalReason.ALREADY_EXECUTED,
                "plan is already marked executed",
            )
        if (
            request.action_plan_id in self._attempted
            or self._durable_attempt_exists(request.action_plan_id)
        ):
            return (
                RefusalReason.DUPLICATE_ATTEMPT,
                "an execution attempt already exists for this plan",
            )

        observation = request.fresh_observation
        if observation is None:
            return (
                RefusalReason.STALE_OBSERVATION,
                "no fresh observation is attached to the request",
            )
        if observation.resource_id != request.resource_id:
            return (
                RefusalReason.RESOURCE_IDENTITY_MISMATCH,
                "fresh observation targets a different resource",
            )
        if observation.observed_at < request.snapshot.created_at:
            return (
                RefusalReason.STALE_OBSERVATION,
                "fresh observation predates the snapshot",
            )
        if observation.resource_type is not resource.resource_type:
            return (
                RefusalReason.RESOURCE_IDENTITY_MISMATCH,
                "fresh observation reports a different resource type",
            )
        if (
            resource.arn is not None
            and observation.arn is not None
            and observation.arn != resource.arn
        ):
            return (
                RefusalReason.RESOURCE_IDENTITY_MISMATCH,
                "fresh observation ARN does not match the snapshot record",
            )
        if (
            resource.account_id is not None
            and observation.account_id is not None
            and observation.account_id != resource.account_id
        ):
            return (
                RefusalReason.RESOURCE_IDENTITY_MISMATCH,
                "fresh observation account id does not match the snapshot record",
            )
        return None

    def _ticket_gate(
        self, request: ExecutionRequest
    ) -> tuple[RefusalReason, str] | None:
        ticket = request.ticket
        if ticket is None:
            return (
                RefusalReason.MISSING_TICKET,
                "approval is required but no ticket is attached",
            )
        try:
            stored = self._store.get(ticket.ticket_id)
        except UnknownTicketError:
            return (
                RefusalReason.MISSING_TICKET,
                "attached ticket is not present in the approval store",
            )
        if stored.status is not ApprovalStatus.GRANTED:
            if stored.status is ApprovalStatus.PENDING:
                return (
                    RefusalReason.TICKET_PENDING,
                    "attached ticket is still pending approval",
                )
            if stored.status is ApprovalStatus.DENIED:
                return (
                    RefusalReason.TICKET_DENIED,
                    "attached ticket was denied",
                )
            return (
                RefusalReason.TICKET_EXPIRED,
                "attached ticket has expired",
            )
        if stored.resource_id != request.resource_id:
            return (
                RefusalReason.TICKET_MISMATCH_RESOURCE,
                "ticket approves a different resource",
            )
        if stored.action is not request.action:
            return (
                RefusalReason.TICKET_MISMATCH_ACTION,
                "ticket approves a different action",
            )
        if stored.plan_id is not request.action_plan_id:
            return (
                RefusalReason.TICKET_MISMATCH_PLAN,
                "ticket is not bound to this plan",
            )
        if stored.consumed:
            return (
                RefusalReason.TICKET_CONSUMED,
                "ticket was already consumed by another attempt",
            )
        return None

    def _decision_gate(
        self, request: ExecutionRequest
    ) -> tuple[RefusalReason, str] | None:
        decision = request.decision
        if decision.resource_id != request.resource_id:
            return (
                RefusalReason.DECISION_MISMATCH,
                "decision targets a different resource",
            )
        if decision.recommended_action is not request.action:
            return (
                RefusalReason.DECISION_MISMATCH,
                "decision recommends a different action",
            )
        if (
            request.plan.decision_id is not None
            and request.plan.decision_id != decision.decision_id
        ):
            return (
                RefusalReason.DECISION_MISMATCH,
                "plan is stamped with a different decision",
            )
        if (
            decision.snapshot_id is not None
            and decision.snapshot_id != request.snapshot.snapshot_id
        ):
            return (
                RefusalReason.SNAPSHOT_MISMATCH,
                "decision is not derived from the attached snapshot",
            )
        if (
            decision.run_id is not None
            and request.snapshot.run_id is not None
            and decision.run_id != request.snapshot.run_id
        ):
            return (
                RefusalReason.SNAPSHOT_MISMATCH,
                "decision run does not match the attached snapshot run",
            )
        return None

    def _durable_attempt_exists(self, action_plan_id: str) -> bool:
        if self._audit_store is None:
            return False
        for envelope in self._audit_store.records():
            if envelope.kind is not AuditRecordKind.EXECUTION:
                continue
            if envelope.action_plan_id != action_plan_id:
                continue
            stage = (envelope.payload or {}).get("stage")
            if stage in ("attempt", "result", "post"):
                return True
        return False

    def _write(
        self,
        *,
        stage: ExecutionStage,
        request: ExecutionRequest,
        execution_id: str,
        outcome: ExecutionOutcome | None = None,
        attempt: MutationAttempt | None = None,
        verification: Any | None = None,
        consumed: bool = False,
        note: str = "",
    ) -> None:
        if self._audit_store is None:
            return
        snapshot = request.snapshot
        decision = request.decision
        self._audit_store.write(
            kind=AuditRecordKind.EXECUTION,
            run_id=snapshot.run_id if snapshot is not None else None,
            snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
            decision_id=decision.decision_id if decision is not None else None,
            action_plan_id=request.action_plan_id,
            ticket_id=(
                request.ticket.ticket_id if request.ticket is not None else None
            ),
            resource_id=request.resource_id,
            payload=execution_payload(
                stage=stage,
                execution_id=execution_id,
                request=request,
                outcome=outcome,
                attempt=attempt,
                verification=verification,
                consumed=consumed,
                note=note,
            ),
        )

    @staticmethod
    def _find_resource(
        snapshot: WorkspaceSnapshot, resource_id: str
    ):
        for record in snapshot.resources:
            if record.resource_id == resource_id:
                return record
        return None


def _outcome_for_status(
    status: VerificationStatus | None,
) -> ExecutionOutcome:
    if status is VerificationStatus.SUCCESS:
        return ExecutionOutcome.VERIFIED_SUCCESS
    if status is VerificationStatus.FAILED:
        return ExecutionOutcome.FAILED
    if status is VerificationStatus.PARTIALLY_VERIFIED:
        return ExecutionOutcome.PARTIALLY_VERIFIED
    return ExecutionOutcome.UNKNOWN