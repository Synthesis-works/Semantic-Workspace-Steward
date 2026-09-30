"""M9 execution-safety gate: registry, coordinator, ledger, and hermetic guards.

M9 builds the execution machinery but deliberately registers no mutation
handler: anything that crosses the gate in production ends NOT_EXECUTED.
These tests pin that boundary, the full gate refusal matrix, idempotency,
the PRE -> ATTEMPT -> RESULT -> POST audit contract, workflow decision
lineage, ticket consumption, and the hermetic source-scan guards that keep
the execution and verification modules free of any AWS client surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from types import MappingProxyType

import pytest

from sws_agent.approval import (
    InMemoryApprovalStore,
    InvalidTransitionError,
    UnknownTicketError,
)
from sws_agent.audit import AuditRecordKind, JsonlAuditStore
from sws_agent.constants import (
    ApprovalStatus,
    ExecutionMode,
    ExecutionOutcome,
    ExecutionStage,
    PotentialAction,
    RefusalReason,
    RiskLevel,
    SWSResourceType,
    VerificationStatus,
    SWS_SUPPORTED_EXECUTION_OUTCOMES,
    SWS_SUPPORTED_EXECUTION_STAGES,
    SWS_SUPPORTED_REFUSAL_REASONS,
    SWS_SUPPORTED_VERIFICATION_STATUSES,
)
from sws_agent.execution import ACTION_EXECUTION_REGISTRY, ExecutionCoordinator
from sws_agent.verification import ObservationError
from sws_agent.models import (
    ActionPlan,
    ApprovalTicket,
    ExecutionRequest,
    MutationAttempt,
    PolicyDecision,
    ResourceObservation,
    ResourceRecord,
    WorkspaceSnapshot,
)
from sws_agent.workflow import ActionPlanner

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

EC2_ARNS = "arn:aws:ec2:us-east-1:123456789012:instance/i-abc123"
ACCOUNT_ID = "123456789012"

STOP = PotentialAction.STOP_RESOURCE
SAFE = ExecutionMode.SAFE

REFUSED = ExecutionOutcome.REFUSED


class _Clock:
    def __init__(self) -> None:
        self._value = FIXED_NOW

    def __call__(self) -> datetime:
        return self._value

    def advance(self, seconds: int) -> None:
        self._value = self._value + timedelta(seconds=seconds)


class _SpyAuditStore:
    """In-memory stand-in for the AuditStore protocol (no filesystem)."""

    def __init__(self) -> None:
        self.writes: list[tuple[AuditRecordKind, dict]] = []

    def write(self, kind: AuditRecordKind, *, _only=False, **kwargs) -> object:
        del _only
        self.writes.append((kind, kwargs))
        return object()

    def records(self) -> list:
        return []

    def close(self) -> None:
        return None

    def __len__(self) -> int:
        return len(self.writes)


def _make_snapshot(*, partial: bool = False, truncated: bool = False) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        snapshot_id="snap-1",
        run_id="run-1",
        created_at=FIXED_NOW - timedelta(minutes=10),
        regions=["us-east-1"],
        resource_types=[SWSResourceType.EC2_INSTANCE],
        resources=[
            ResourceRecord(
                resource_id="inst-1",
                resource_type=SWSResourceType.EC2_INSTANCE,
                name="instance-one",
                arn=EC2_ARNS,
                account_id=ACCOUNT_ID,
            )
        ],
        counts={SWSResourceType.EC2_INSTANCE: 1},
        partial=partial,
        truncated=truncated,
    )


def _make_decision(snapshot: WorkspaceSnapshot) -> PolicyDecision:
    return PolicyDecision(
        resource_id="inst-1",
        recommended_action=STOP,
        risk_level=RiskLevel.MEDIUM,
        needs_approval=True,
        decision_id="dec-1",
        snapshot_id=snapshot.snapshot_id,
        run_id=snapshot.run_id,
    )


def _make_plan(
    store: InMemoryApprovalStore,
    *,
    snapshot: WorkspaceSnapshot,
    decision: PolicyDecision,
    mode: ExecutionMode = SAFE,
) -> ActionPlan:
    return ActionPlanner(
        approval_store=store,
        execution_mode=mode,
        plan_id_source=lambda: "plan-1",
        now=lambda: FIXED_NOW,
    ).plan(
        resource_id="inst-1",
        resource_type=SWSResourceType.EC2_INSTANCE,
        action=STOP,
        decision=decision,
    )


def _grant(store: InMemoryApprovalStore, plan: ActionPlan) -> ApprovalTicket:
    return store.grant(plan.ticket.ticket_id, decided_by="human-1")


def _observation(
    *,
    resource_id: str = "inst-1",
    resource_type: SWSResourceType = SWSResourceType.EC2_INSTANCE,
    observed_at: datetime | None = None,
    facts: dict | None = None,
    arn: str | None = EC2_ARNS,
    account_id: str | None = ACCOUNT_ID,
) -> ResourceObservation:
    return ResourceObservation(
        resource_id=resource_id,
        resource_type=resource_type,
        facts=dict(facts or {}),
        observed_at=observed_at or (FIXED_NOW + timedelta(minutes=1)),
        arn=arn,
        account_id=account_id,
    )


def _request(
    *,
    plan: ActionPlan,
    snapshot: WorkspaceSnapshot,
    decision: PolicyDecision,
    ticket: ApprovalTicket | None,
    observation: ResourceObservation | None = None,
    expected_poststate: dict | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        action_plan_id="plan-1",
        resource_id="inst-1",
        action=STOP,
        execution_mode=plan.execution_mode,
        plan=plan,
        decision=decision,
        snapshot=snapshot,
        ticket=ticket,
        fresh_observation=observation,
        expected_poststate=dict(expected_poststate or {}),
    )


class _World:
    """The fake post-mutation world the fake handler and observer share."""

    def __init__(self) -> None:
        self.state = "running"
        self.handler_calls: list[ExecutionRequest] = []
        self.timeout = False
        self.call_error = False

    def handle(self, request: ExecutionRequest) -> MutationAttempt:
        self.handler_calls.append(request)
        if self.timeout:
            return MutationAttempt(ambiguous=True, sanitized={"note": "timeout"})
        if self.call_error:
            return MutationAttempt(call_error=True, sanitized={"note": "api error"})
        self.state = "stopped"
        return MutationAttempt(sanitized={"dispatched": True})

    def observe(self, resource_id: str) -> ResourceObservation:
        return ResourceObservation(
            resource_id=resource_id,
            resource_type=SWSResourceType.EC2_INSTANCE,
            facts={"state": self.state},
            observed_at=FIXED_NOW + timedelta(minutes=1),
            arn=EC2_ARNS,
            account_id=ACCOUNT_ID,
        )


class _RaisingObserver:
    def observe(self, resource_id: str) -> None:
        del resource_id
        raise ObservationError("simulated failure")


class _SeqIds:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._counter = count(1)

    def __call__(self) -> str:
        return f"{self._prefix}-{next(self._counter)}"


def _jsonl_audit(path: Path, clock: _Clock) -> JsonlAuditStore:
    return JsonlAuditStore(path, now=clock, id_source=_SeqIds("audit"))


# ---------------------------------------------------------------------------
# Registry: metadata only, immutable, nothing implemented.
# ---------------------------------------------------------------------------


def test_registry_covers_every_potential_action():
    assert set(ACTION_EXECUTION_REGISTRY) == set(PotentialAction)


def test_registry_is_immutable():
    with pytest.raises(TypeError):
        ACTION_EXECUTION_REGISTRY[PotentialAction.LEAVE] = ACTION_EXECUTION_REGISTRY[
            PotentialAction.LEAVE
        ]
    assert isinstance(ACTION_EXECUTION_REGISTRY, MappingProxyType)


def test_registry_holds_no_implemented_mutations():
    assert all(not spec.implemented for spec in ACTION_EXECUTION_REGISTRY.values())


def test_registry_mutation_is_documentation_only_never_callable():
    for spec in ACTION_EXECUTION_REGISTRY.values():
        assert spec.mutation is None or isinstance(spec.mutation, str)


def test_stop_resource_contract_in_registry():
    spec = ACTION_EXECUTION_REGISTRY[STOP]
    assert spec.eligible_resource_types == frozenset({SWSResourceType.EC2_INSTANCE})
    assert spec.requires_human_approval is True
    assert spec.implemented is False
    assert "ec2:StopInstances" in spec.mutation


def test_non_executable_actions_have_no_eligible_resource_types():
    for action in (
        PotentialAction.LEAVE,
        PotentialAction.FLAG_FOR_REVIEW,
        PotentialAction.REQUEST_APPROVAL,
    ):
        spec = ACTION_EXECUTION_REGISTRY[action]
        assert not spec.eligible_resource_types
        assert spec.requires_human_approval is False
        assert spec.mutation is None


# ---------------------------------------------------------------------------
# Gate refusal matrix (each refusal performs no handler call and no audit write).
# ---------------------------------------------------------------------------


def _refusal_context(**overrides) -> tuple[ExecutionRequest, InMemoryApprovalStore]:
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    defaults = dict(
        action_plan_id="plan-1",
        resource_id="inst-1",
        action=STOP,
        execution_mode=SAFE,
        plan=plan,
        decision=decision,
        snapshot=snapshot,
        ticket=_grant(store, plan),
        fresh_observation=_observation(facts={"state": "running"}),
    )
    defaults.update(overrides)
    return ExecutionRequest(**defaults), store


def _refused_request_gate(
    reason: RefusalReason, request: ExecutionRequest, store: InMemoryApprovalStore
) -> None:
    audit = _SpyAuditStore()
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is REFUSED
    assert result.refusal is reason
    assert result.attempt_id is None
    assert world.handler_calls == []
    assert len(audit) == 0


def _refused(*, reason: RefusalReason, **overrides) -> None:
    request, store = _refusal_context(**overrides)
    _refused_request_gate(reason, request, store)


def test_refusal_missing_plan():
    _refused(reason=RefusalReason.MISSING_PLAN, plan=None)


def test_refusal_plan_id_mismatch():
    _refused(reason=RefusalReason.MISSING_PLAN, action_plan_id="other-plan")


def test_refusal_missing_decision():
    _refused(reason=RefusalReason.MISSING_DECISION, decision=None)


def test_refusal_missing_snapshot():
    _refused(reason=RefusalReason.MISSING_SNAPSHOT, snapshot=None)


def test_refusal_mode_mismatch():
    _refused(reason=RefusalReason.MODE_MISMATCH, execution_mode=ExecutionMode.REVIEW)


def test_refusal_action_not_executable():
    _refused(reason=RefusalReason.ACTION_NOT_EXECUTABLE, action=PotentialAction.LEAVE)


def test_refusal_resource_not_in_snapshot():
    _refused(reason=RefusalReason.RESOURCE_NOT_IN_SNAPSHOT, resource_id="missing-id")


def test_refusal_resource_type_mismatch():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot().model_copy(
        update={
            "resource_types": [SWSResourceType.S3_BUCKET],
            "resources": [
                ResourceRecord(
                    resource_id="bucket-1",
                    resource_type=SWSResourceType.S3_BUCKET,
                    name="bucket-one",
                    arn="arn:aws:s3:::bucket-one",
                )
            ],
        }
    )
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    request = ExecutionRequest(
        action_plan_id="plan-1",
        resource_id="bucket-1",
        action=STOP,
        execution_mode=SAFE,
        plan=plan,
        decision=decision,
        snapshot=snapshot,
        ticket=_grant(store, plan),
        fresh_observation=_observation(
            resource_id="bucket-1",
            resource_type=SWSResourceType.S3_BUCKET,
            arn="arn:aws:s3:::bucket-one",
            account_id=None,
        ),
    )
    _refused_request_gate(RefusalReason.RESOURCE_TYPE_MISMATCH, request, store)


def test_refusal_partial_snapshot():
    _refused(reason=RefusalReason.PARTIAL_SNAPSHOT, snapshot=_make_snapshot(partial=True))


def test_refusal_truncated_snapshot():
    _refused(
        reason=RefusalReason.TRUNCATED_SNAPSHOT, snapshot=_make_snapshot(truncated=True)
    )


def test_refusal_autonomous_execution_unsupported():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision, mode=ExecutionMode.AUTONOMOUS)
    request = ExecutionRequest(
        action_plan_id="plan-1",
        resource_id="inst-1",
        action=STOP,
        execution_mode=ExecutionMode.AUTONOMOUS,
        plan=plan,
        decision=decision,
        snapshot=snapshot,
        ticket=None,
        fresh_observation=_observation(facts={"state": "running"}),
    )
    _refused_request_gate(RefusalReason.AUTONOMOUS_EXECUTION_UNSUPPORTED, request, store)


def test_safe_mode_rejects_stop_without_granted_ticket():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=store.get(plan.ticket.ticket_id),
        observation=_observation(facts={"state": "running"}),
    )
    _refused_request_gate(RefusalReason.TICKET_PENDING, request, store)


def test_refusal_missing_ticket():
    _refused(reason=RefusalReason.MISSING_TICKET, ticket=None)


def test_refusal_unknown_ticket_presented_as_missing():
    request, store = _refusal_context(
        ticket=ApprovalTicket(
            ticket_id="ghost-ticket",
            resource_id="inst-1",
            action=STOP,
            created_at=FIXED_NOW,
        )
    )
    _refused_request_gate(RefusalReason.MISSING_TICKET, request, store)


def test_refusal_ticket_denied():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    denied = store.deny(plan.ticket.ticket_id, decided_by="human-1")
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=denied,
        observation=_observation(facts={"state": "running"}),
    )
    _refused_request_gate(RefusalReason.TICKET_DENIED, request, store)


def test_refusal_ticket_expired():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    expired = store.expire(plan.ticket.ticket_id, decided_by="human-1")
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=expired,
        observation=_observation(facts={"state": "running"}),
    )
    _refused_request_gate(RefusalReason.TICKET_EXPIRED, request, store)


def _mismatch_ticket(
    store: InMemoryApprovalStore,
    *,
    resource_id: str,
    action: PotentialAction,
    plan_id: str = "plan-1",
) -> ApprovalTicket:
    ticket = store.create_ticket(
        resource_id=resource_id,
        action=action,
        plan_id=plan_id,
        ticket_id="other-ticket",
    )
    return store.grant(ticket.ticket_id, decided_by="human-1")


def test_refusal_ticket_mismatch_resource():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    other = _mismatch_ticket(
        store, resource_id="other-inst", action=STOP, plan_id="plan-1"
    )
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=other,
        observation=_observation(facts={"state": "running"}),
    )
    _refused_request_gate(RefusalReason.TICKET_MISMATCH_RESOURCE, request, store)


def test_refusal_ticket_mismatch_action():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    other = _mismatch_ticket(
        store, resource_id="inst-1", action=PotentialAction.LEAVE, plan_id="plan-1"
    )
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=other,
        observation=_observation(facts={"state": "running"}),
    )
    _refused_request_gate(RefusalReason.TICKET_MISMATCH_ACTION, request, store)


def test_refusal_ticket_mismatch_plan():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    other = _mismatch_ticket(
        store, resource_id="inst-1", action=STOP, plan_id="other-plan"
    )
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=other,
        observation=_observation(facts={"state": "running"}),
    )
    _refused_request_gate(RefusalReason.TICKET_MISMATCH_PLAN, request, store)


def test_refusal_ticket_consumed():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    store.consume(_grant(store, plan).ticket_id)
    consumed = store.get(plan.ticket.ticket_id)
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=consumed,
        observation=_observation(facts={"state": "running"}),
    )
    _refused_request_gate(RefusalReason.TICKET_CONSUMED, request, store)


def test_refusal_decision_mismatch_resource():
    request, store = _refusal_context(
        decision=_make_decision(_make_snapshot()).model_copy(
            update={"resource_id": "other-inst"}
        )
    )
    _refused_request_gate(RefusalReason.DECISION_MISMATCH, request, store)


def test_refusal_decision_mismatch_action():
    request, store = _refusal_context(
        decision=_make_decision(_make_snapshot()).model_copy(
            update={"recommended_action": PotentialAction.LEAVE}
        )
    )
    _refused_request_gate(RefusalReason.DECISION_MISMATCH, request, store)


def test_refusal_plan_stamped_with_different_decision():
    request, store = _refusal_context()
    request.plan = request.plan.model_copy(
        update={"decision_id": "different-decision"}
    )
    _refused_request_gate(RefusalReason.DECISION_MISMATCH, request, store)


def test_refusal_decision_snapshot_mismatch():
    request, store = _refusal_context(
        decision=_make_decision(_make_snapshot()).model_copy(
            update={"snapshot_id": "snap-other"}
        )
    )
    _refused_request_gate(RefusalReason.SNAPSHOT_MISMATCH, request, store)


def test_refusal_decision_run_mismatch():
    request, store = _refusal_context(
        decision=_make_decision(_make_snapshot()).model_copy(update={"run_id": "run-other"})
    )
    _refused_request_gate(RefusalReason.SNAPSHOT_MISMATCH, request, store)


def test_refusal_already_executed_plan():
    request, store = _refusal_context()
    request.plan = request.plan.model_copy(update={"executed": True})
    _refused_request_gate(RefusalReason.ALREADY_EXECUTED, request, store)


def test_refusal_stale_observation_missing():
    _refused(reason=RefusalReason.STALE_OBSERVATION, fresh_observation=None)


def test_refusal_observation_predates_snapshot():
    _refused(
        reason=RefusalReason.STALE_OBSERVATION,
        fresh_observation=_observation(observed_at=FIXED_NOW - timedelta(days=1)),
    )


def test_refusal_observation_identity_resource_mismatch():
    _refused(
        reason=RefusalReason.RESOURCE_IDENTITY_MISMATCH,
        fresh_observation=_observation(resource_id="other-inst"),
    )


def test_refusal_observation_identity_type_mismatch():
    _refused(
        reason=RefusalReason.RESOURCE_IDENTITY_MISMATCH,
        fresh_observation=_observation(resource_type=SWSResourceType.S3_BUCKET),
    )


def test_refusal_observation_identity_arn_mismatch():
    _refused(
        reason=RefusalReason.RESOURCE_IDENTITY_MISMATCH,
        fresh_observation=_observation(
            arn="arn:aws:ec2:us-east-1:999999999999:instance/x"
        ),
    )


def test_refusal_observation_identity_account_mismatch():
    _refused(
        reason=RefusalReason.RESOURCE_IDENTITY_MISMATCH,
        fresh_observation=_observation(account_id="999999999999"),
    )


# ---------------------------------------------------------------------------
# M9 honesty: gate passed but nothing executes, and nothing is claimed.
# ---------------------------------------------------------------------------


def _gated_request_with_ticket(
    store: InMemoryApprovalStore,
) -> tuple[ExecutionRequest, ActionPlan, WorkspaceSnapshot]:
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=_grant(store, plan),
        observation=_observation(facts={"state": "running"}),
        expected_poststate={"state": "stopped"},
    )
    return request, plan, snapshot


def test_gate_pass_with_no_handler_reports_not_executed():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    coordinator = ExecutionCoordinator(
        approval_store=store,
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.NOT_EXECUTED
    assert result.refusal is None
    assert result.verification is None
    assert result.attempt_id is None
    assert "nothing was executed" in result.note


def test_not_executed_does_not_consume_ticket():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    coordinator = ExecutionCoordinator(
        approval_store=store,
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    coordinator.execute(request)
    assert store.get(request.ticket.ticket_id).consumed is False


def test_not_executed_writes_pre_only():
    store = InMemoryApprovalStore(now=_Clock())
    audit = _SpyAuditStore()
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    coordinator = ExecutionCoordinator(
        approval_store=store,
        audit_store=audit,  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    coordinator.execute(request)
    assert len(audit) == 1
    kind, kwargs = audit.writes[0]
    assert kind is AuditRecordKind.EXECUTION
    assert kwargs["payload"]["stage"] == ExecutionStage.PRE.value
    assert kwargs["payload"].get("outcome") is None
    assert kwargs["payload"]["consumed"] is False


# ---------------------------------------------------------------------------
# Gated execution with an injected fake handler: outcomes + idempotency.
# ---------------------------------------------------------------------------


def test_verified_success_when_observation_matches():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert result.verification is VerificationStatus.SUCCESS
    assert result.attempt_id == "exec-1"
    assert len(world.handler_calls) == 1
    assert store.get(request.ticket.ticket_id).consumed is True


def test_partially_verified_when_some_expected_facts_unobserved():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=_grant(store, plan),
        observation=_observation(facts={"state": "running"}),
        expected_poststate={"state": "stopped", "instance_type": "t3.micro"},
    )
    world = _World()

    class _PartialObserver:
        def observe(self, resource_id: str) -> ResourceObservation:
            return ResourceObservation(
                resource_id=resource_id,
                resource_type=SWSResourceType.EC2_INSTANCE,
                facts={"state": "stopped"},
                observed_at=FIXED_NOW + timedelta(minutes=1),
                arn=EC2_ARNS,
                account_id=ACCOUNT_ID,
            )

    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=_PartialObserver(),
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.PARTIALLY_VERIFIED
    assert result.verification is VerificationStatus.PARTIALLY_VERIFIED


def test_call_error_reports_failed():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    world.call_error = True
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.FAILED
    assert result.verification is VerificationStatus.FAILED
    assert store.get(request.ticket.ticket_id).consumed is True


def test_timeout_is_unknown_never_failed():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    world.timeout = True
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.UNKNOWN
    assert result.verification is VerificationStatus.UNKNOWN


def test_observer_raising_is_unknown():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=_RaisingObserver(),
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.UNKNOWN
    assert result.verification is VerificationStatus.UNKNOWN


def test_no_observer_is_unknown():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=None,
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.UNKNOWN
    assert result.verification is VerificationStatus.UNKNOWN


def test_no_blind_retry_after_ambiguous_attempt():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    world.timeout = True
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    first = coordinator.execute(request)
    assert first.outcome is ExecutionOutcome.UNKNOWN
    world.timeout = False
    refreshed = request.model_copy(
        update={"ticket": _refresh_grant(store, request.ticket.ticket_id)}
    )
    second = coordinator.execute(refreshed)
    assert second.outcome is REFUSED
    assert second.refusal is RefusalReason.DUPLICATE_ATTEMPT
    assert len(world.handler_calls) == 1


def test_duplicate_attempt_detected_across_coordinators_via_ledger(tmp_path: Path):
    clock = _Clock()
    store = InMemoryApprovalStore(now=clock)
    audit = _jsonl_audit(tmp_path / "audit.jsonl", clock)
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    first = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    assert first.execute(request).outcome is ExecutionOutcome.VERIFIED_SUCCESS
    refreshed = request.model_copy(
        update={"ticket": _refresh_grant(store, request.ticket.ticket_id)}
    )
    second = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = second.execute(refreshed)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.DUPLICATE_ATTEMPT


def _refresh_grant(store: InMemoryApprovalStore, ticket_id: str) -> ApprovalTicket:
    stored = store.get(ticket_id)
    if not stored.consumed:
        return stored
    new_ticket = store.create_ticket(
        resource_id="inst-1",
        action=STOP,
        plan_id="plan-1",
        ticket_id="second-ticket",
    )
    return store.grant(new_ticket.ticket_id, decided_by="human-1")


# ---------------------------------------------------------------------------
# Audit contract: PRE -> ATTEMPT -> RESULT -> POST correlation.
# ---------------------------------------------------------------------------


def test_ledger_correlates_the_execution_transaction(tmp_path: Path):
    clock = _Clock()
    store = InMemoryApprovalStore(now=clock)
    audit = _jsonl_audit(tmp_path / "audit.jsonl", clock)
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,
        id_source=lambda: "exec-1",
        now=clock,
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS

    records = audit.records()
    assert [record.payload["stage"] for record in records] == [
        "pre",
        "attempt",
        "result",
        "post",
    ]
    for record in records:
        assert record.kind is AuditRecordKind.EXECUTION
        assert record.execution is None
        assert record.run_id == "run-1"
        assert record.snapshot_id == "snap-1"
        assert record.decision_id == "dec-1"
        assert record.action_plan_id == "plan-1"
        assert record.resource_id == "inst-1"
        assert record.payload["execution_id"] == "exec-1"
        assert record.payload["resource_id"] == "inst-1"
        assert record.payload["action"] == "stop_resource"
        assert record.payload["execution_mode"] == "safe"
    assert records[-1].payload["outcome"] == "verified_success"
    assert records[-1].payload["consumed"] is True
    assert records[-1].payload.get("verification") is None


def test_ledger_records_no_secrets(tmp_path: Path):
    clock = _Clock()
    store = InMemoryApprovalStore(now=clock)
    audit = _jsonl_audit(tmp_path / "audit.jsonl", clock)
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,
        id_source=lambda: "exec-1",
        now=clock,
    )
    coordinator.execute(request)
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "secret" not in raw.lower()
    assert "credential" not in raw.lower()
    assert "aws_access_key" not in raw


# ---------------------------------------------------------------------------
# Workflow decision lineage + approval ticket consumption.
# ---------------------------------------------------------------------------


def test_plan_stamps_decision_lineage():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    assert plan.decision_id == "dec-1"
    assert plan.snapshot_id == "snap-1"
    assert plan.run_id == "run-1"


def test_plan_without_decision_leaves_lineage_unset():
    store = InMemoryApprovalStore(now=_Clock())
    plan = ActionPlanner(
        approval_store=store,
        execution_mode=SAFE,
        plan_id_source=lambda: "plan-1",
        now=lambda: FIXED_NOW,
    ).plan(
        resource_id="inst-1",
        resource_type=SWSResourceType.EC2_INSTANCE,
        action=PotentialAction.LEAVE,
    )
    assert plan.decision_id is None
    assert plan.snapshot_id is None
    assert plan.run_id is None


def test_plan_rejects_decision_for_other_resource():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot).model_copy(update={"resource_id": "other"})
    with pytest.raises(ValueError):
        _make_plan(store, snapshot=snapshot, decision=decision)


def test_consume_unknown_ticket_raises():
    store = InMemoryApprovalStore(now=_Clock())
    with pytest.raises(UnknownTicketError):
        store.consume("no-such-ticket")


def test_consume_non_granted_raises():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    with pytest.raises(InvalidTransitionError):
        store.consume(plan.ticket.ticket_id)


def test_second_consume_raises():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    store.consume(_grant(store, plan).ticket_id)
    with pytest.raises(InvalidTransitionError):
        store.consume(plan.ticket.ticket_id)


def test_consumed_ticket_stays_granted_but_marked():
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    consumed = store.consume(_grant(store, plan).ticket_id)
    assert consumed.status is ApprovalStatus.GRANTED
    assert consumed.consumed is True


# ---------------------------------------------------------------------------
# Hermetic guards: execution + verification stay free of any AWS client surface.
# ---------------------------------------------------------------------------

_FORBIDDEN_TOKENS = (
    "boto3",
    "botocore",
    "session",
    "client(",
    "importlib",
    "__import__",
    "eval(",
    "exec(",
    "getattr(",
)


def _module_source(name: str) -> str:
    return (Path(__file__).parent.parent / f"src/sws_agent/{name}.py").read_text(
        encoding="utf-8"
    )


def test_execution_module_source_is_hermetic():
    source = _module_source("execution")
    for token in _FORBIDDEN_TOKENS:
        assert token not in source, f"execution.py must not contain {token!r}"


def test_verification_module_source_is_hermetic():
    source = _module_source("verification")
    for token in _FORBIDDEN_TOKENS:
        assert token not in source, f"verification.py must not contain {token!r}"


# ---------------------------------------------------------------------------
# Canonical vocabulary for the new enums.
# ---------------------------------------------------------------------------


def test_new_execution_vocabulary_is_canonical():
    assert SWS_SUPPORTED_EXECUTION_STAGES == {s.value for s in ExecutionStage}
    assert SWS_SUPPORTED_EXECUTION_OUTCOMES == {o.value for o in ExecutionOutcome}
    assert SWS_SUPPORTED_REFUSAL_REASONS == {r.value for r in RefusalReason}
    assert SWS_SUPPORTED_VERIFICATION_STATUSES == {
        v.value for v in VerificationStatus
    }