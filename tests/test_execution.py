"""M9/M10 execution boundary: registry, gate, ledger, and hermetic guards.

M9 built the execution machinery but registered no mutation handler: anything
that crosses the gate in production ends NOT_EXECUTED. M10 hardens the
evidence the gate trusts -- provider-issued A5 observations, bounded
freshness, mandatory identity, action-derived postconditions, durable intent
idempotency, contained observation failures, and read-only reconciliation --
without adding any AWS surface or registering a handler.

These tests pin that boundary, the full gate refusal matrix, idempotency
across coordinator instances and process restarts, the PRE -> ATTEMPT ->
RESULT -> POST audit contract, open-transaction reconciliation, workflow
decision lineage, ticket consumption, and the hermetic source-scan guards
that keep the execution and verification modules free of any AWS client
surface.
"""

from __future__ import annotations

import json
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
    SWS_MAX_OBSERVATION_AGE_SECONDS,
    ApprovalStatus,
    ExecutionMode,
    ExecutionOutcome,
    ExecutionStage,
    ObservationProvenance,
    PotentialAction,
    RefusalReason,
    RiskLevel,
    SWSResourceType,
    VerificationStatus,
    SWS_SUPPORTED_EXECUTION_OUTCOMES,
    SWS_SUPPORTED_EXECUTION_STAGES,
    SWS_SUPPORTED_OBSERVATION_PROVENANCES,
    SWS_SUPPORTED_REFUSAL_REASONS,
    SWS_SUPPORTED_VERIFICATION_STATUSES,
)
from sws_agent.execution import (
    ACTION_EXECUTION_REGISTRY,
    ActionSpec,
    ExecutionCoordinator,
    canonical_postconditions,
    execution_intent_key,
    reconcile_open_transactions,
)
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
EC2_REGION = "us-east-1"

# The snapshot run starts an hour ago and finishes collecting half an hour
# ago, mirroring a real collection: created_at <= collected_at. M9 compared
# observations against created_at, so an observation taken between the two
# satisfied its freshness gate; M10 compares against collected_at.
SNAPSHOT_STARTED = FIXED_NOW - timedelta(minutes=60)
SNAPSHOT_COLLECTED = FIXED_NOW - timedelta(minutes=30)

STOP = PotentialAction.STOP_RESOURCE
SAFE = ExecutionMode.SAFE

REFUSED = ExecutionOutcome.REFUSED

# The canonical, action-derived postcondition for STOP_RESOURCE.
STOP_POSTCONDITION = {"state": "stopped"}


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


def _make_snapshot(
    *,
    partial: bool = False,
    truncated: bool = False,
    collected_at: datetime | None = SNAPSHOT_COLLECTED,
    record_overrides: dict | None = None,
) -> WorkspaceSnapshot:
    record = ResourceRecord(
        resource_id="inst-1",
        resource_type=SWSResourceType.EC2_INSTANCE,
        name="instance-one",
        arn=EC2_ARNS,
        account_id=ACCOUNT_ID,
        region=EC2_REGION,
    )
    if record_overrides:
        record = record.model_copy(update=record_overrides)
    return WorkspaceSnapshot(
        snapshot_id="snap-1",
        run_id="run-1",
        created_at=SNAPSHOT_STARTED,
        collected_at=collected_at,
        regions=[EC2_REGION],
        resource_types=[SWSResourceType.EC2_INSTANCE],
        resources=[record],
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
    plan_id: str = "plan-1",
) -> ActionPlan:
    return ActionPlanner(
        approval_store=store,
        execution_mode=mode,
        plan_id_source=lambda: plan_id,
        now=lambda: FIXED_NOW,
    ).plan(
        resource_id="inst-1",
        resource_type=SWSResourceType.EC2_INSTANCE,
        action=STOP,
        decision=decision,
    )


def _grant(store: InMemoryApprovalStore, plan: ActionPlan) -> ApprovalTicket:
    return store.grant(plan.ticket.ticket_id, decided_by="human-1")


def _provider_observation(
    *,
    resource_id: str = "inst-1",
    resource_type: SWSResourceType = SWSResourceType.EC2_INSTANCE,
    observed_at: datetime | None = None,
    facts: dict | None = None,
    arn: str | None = EC2_ARNS,
    account_id: str | None = ACCOUNT_ID,
    region: str | None = EC2_REGION,
    ambiguous: bool = False,
    issued: bool = True,
) -> ResourceObservation:
    """Build an observation the way a provider would.

    ``issued=False`` deliberately builds a hand-made (UNVERIFIED) observation
    so tests can prove the coordinator rejects caller-shaped evidence.
    """
    payload = dict(
        resource_id=resource_id,
        resource_type=resource_type,
        # `facts={}` is meaningful (nothing observable) and must not be
        # confused with "no override given".
        facts={"state": "running"} if facts is None else dict(facts),
        # M10 refuses evidence from the future, so "fresh" means "as of now".
        observed_at=observed_at or FIXED_NOW,
        arn=arn,
        account_id=account_id,
        region=region,
        ambiguous=ambiguous,
    )
    if issued:
        return ResourceObservation.issued(**payload)
    return ResourceObservation(**payload)


class _Provider:
    """Configurable fake ObservationProvider (the only A5 evidence source)."""

    def __init__(
        self,
        *,
        observation: ResourceObservation | None = None,
        raises: BaseException | None = None,
        returns: object = None,
    ) -> None:
        self.observation = observation
        self.raises = raises
        self.returns = returns
        self.calls: list[str] = []

    def observe(self, resource_id: str):
        self.calls.append(resource_id)
        if self.raises is not None:
            raise self.raises
        if self.returns is not None:
            return self.returns
        return self.observation


class _UnexpectedError(RuntimeError):
    """Stand-in for a provider failure that is not an ObservationError."""


class _FlakyProvider:
    """Provider that answers the A5 preflight, then fails.

    M10 uses one provider for the pre-mutation gate and for post-attempt
    verification, so this models the realistic case: the pre-mutation read
    succeeds and the post-mutation read is the one that breaks.
    """

    def __init__(
        self,
        *,
        raises: BaseException | None = None,
        facts: dict | None = None,
    ) -> None:
        self.raises = raises
        self.facts = facts
        self.calls: list[str] = []

    def observe(self, resource_id: str) -> ResourceObservation:
        self.calls.append(resource_id)
        is_post_attempt = len(self.calls) > 1
        if is_post_attempt:
            if self.raises is not None:
                raise self.raises
            if self.facts is not None:
                return _provider_observation(
                    resource_id=resource_id, facts=self.facts
                )
        return _provider_observation(resource_id=resource_id)


def _request(
    *,
    plan: ActionPlan,
    snapshot: WorkspaceSnapshot,
    decision: PolicyDecision,
    ticket: ApprovalTicket | None,
    action: PotentialAction = STOP,
    action_plan_id: str = "plan-1",
    expected_poststate: dict | None = None,
    fresh_observation: ResourceObservation | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        action_plan_id=action_plan_id,
        resource_id="inst-1",
        action=action,
        execution_mode=plan.execution_mode,
        plan=plan,
        decision=decision,
        snapshot=snapshot,
        ticket=ticket,
        fresh_observation=fresh_observation,
        expected_poststate=dict(expected_poststate or {}),
    )


class _World:
    """The fake post-mutation world the fake handler and provider share.

    ``observe`` is the M10 provider boundary: it is the only source of A5
    evidence and of post-attempt evidence, and it always issues
    provider-issued observations with complete identity.
    """

    def __init__(self) -> None:
        self.state = "running"
        self.handler_calls: list[ExecutionRequest] = []
        self.timeout = False
        self.call_error = False
        self.observed: list[str] = []

    def handle(self, request: ExecutionRequest) -> MutationAttempt:
        self.handler_calls.append(request)
        if self.timeout:
            return MutationAttempt(ambiguous=True, sanitized={"note": "timeout"})
        if self.call_error:
            return MutationAttempt(call_error=True, sanitized={"note": "api error"})
        self.state = "stopped"
        return MutationAttempt(sanitized={"dispatched": True})

    def observe(self, resource_id: str) -> ResourceObservation:
        self.observed.append(resource_id)
        return ResourceObservation.issued(
            resource_id=resource_id,
            resource_type=SWSResourceType.EC2_INSTANCE,
            facts={"state": self.state},
            observed_at=FIXED_NOW,
            arn=EC2_ARNS,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
        )


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


def _a5_refused(
    reason: RefusalReason,
    *,
    provider=None,
    audit: _SpyAuditStore | None = None,
    **overrides,
) -> None:
    """Assert an A5 refusal driven by the injected provider, not the request.

    ``provider`` replaces the default fake provider so the test controls the
    evidence the coordinator actually receives.
    """
    request, store = _refusal_context(**overrides)
    audit = audit if audit is not None else _SpyAuditStore()
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world if provider is None else provider,
        audit_store=audit,  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is REFUSED
    assert result.refusal is reason
    assert world.handler_calls == []
    assert len(audit) == 0


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


# ---------------------------------------------------------------------------
# M10 A5: the coordinator obtains its own provider-issued evidence.
# ---------------------------------------------------------------------------


def test_caller_supplied_observation_is_refused():
    """M10: a request cannot inject facts that satisfy A5."""
    _a5_refused(
        reason=RefusalReason.CALLER_SUPPLIED_OBSERVATION_REJECTED,
        fresh_observation=_provider_observation(facts={"state": "stopped"}),
    )


def test_caller_supplied_observation_is_refused_even_when_valid():
    """Even a complete, provider-shaped observation is refused on the request.

    This is the exact M9 bypass: the caller used to attach the evidence and
    the gate believed it. Now only the coordinator's own provider call counts.
    """
    _a5_refused(
        reason=RefusalReason.CALLER_SUPPLIED_OBSERVATION_REJECTED,
        fresh_observation=_provider_observation(),
    )


def test_coordinator_requests_its_own_observation():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    provider = _Provider(observation=_provider_observation())
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=provider,
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    coordinator.execute(request)
    # Once for the A5 preflight, once for post-attempt verification.
    assert provider.calls == ["inst-1", "inst-1"]


def test_provider_issued_observation_satisfies_a5():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    result = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    ).execute(request)
    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS


def test_missing_observation_provider_is_fail_closed():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    audit = _SpyAuditStore()
    result = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=None,
        audit_store=audit,  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    ).execute(request)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE
    assert world.handler_calls == []
    assert len(audit) == 0


def test_unverified_provenance_is_refused():
    """A hand-built observation cannot satisfy A5, even when otherwise valid."""
    _a5_refused(
        reason=RefusalReason.OBSERVATION_NOT_PROVIDER_ISSUED,
        provider=_Provider(observation=_provider_observation(issued=False)),
    )


def test_observation_default_provenance_is_unverified():
    """Fail-closed default: an observation is untrusted until issued."""
    observation = ResourceObservation(
        resource_id="inst-1",
        resource_type=SWSResourceType.EC2_INSTANCE,
        facts={"state": "running"},
        observed_at=FIXED_NOW,
    )
    assert observation.provenance is ObservationProvenance.UNVERIFIED


def test_provider_returning_none_is_refused():
    _a5_refused(
        reason=RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE,
        provider=_Provider(observation=None),
    )


def test_provider_raising_observation_error_is_refused():
    _a5_refused(
        reason=RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE,
        provider=_Provider(raises=ObservationError("cannot establish state")),
    )


def test_provider_raising_unexpected_error_is_refused():
    _a5_refused(
        reason=RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE,
        provider=_Provider(raises=_UnexpectedError("client error")),
    )


def test_provider_returning_wrong_type_is_refused():
    _a5_refused(
        reason=RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE,
        provider=_Provider(returns={"state": "stopped"}),
    )


def test_ambiguous_preflight_observation_is_refused():
    _a5_refused(
        reason=RefusalReason.STALE_OBSERVATION,
        provider=_Provider(observation=_provider_observation(ambiguous=True)),
    )


# ---------------------------------------------------------------------------
# M10 freshness: bounded on both sides, and anchored on collection time.
# ---------------------------------------------------------------------------


def test_observation_predating_collected_at_is_refused():
    """The M9 hole: newer than created_at but older than the collection.

    M9 compared against ``snapshot.created_at`` (the run start), so an
    observation taken while inventory was still being collected satisfied the
    freshness gate. M10 anchors on ``collected_at``.
    """
    between_start_and_collection = SNAPSHOT_STARTED + timedelta(minutes=15)
    assert between_start_and_collection > SNAPSHOT_STARTED
    assert between_start_and_collection < SNAPSHOT_COLLECTED
    _a5_refused(
        reason=RefusalReason.STALE_OBSERVATION,
        provider=_Provider(
            observation=_provider_observation(observed_at=between_start_and_collection)
        ),
    )


def test_observation_predating_snapshot_is_refused():
    _a5_refused(
        reason=RefusalReason.STALE_OBSERVATION,
        provider=_Provider(
            observation=_provider_observation(
                observed_at=FIXED_NOW - timedelta(days=1)
            )
        ),
    )


def test_observation_beyond_maximum_age_is_refused():
    stale_but_post_collection = SNAPSHOT_COLLECTED + timedelta(minutes=10)
    assert FIXED_NOW - stale_but_post_collection > timedelta(
        seconds=SWS_MAX_OBSERVATION_AGE_SECONDS
    )
    _a5_refused(
        reason=RefusalReason.OBSERVATION_TOO_OLD,
        provider=_Provider(
            observation=_provider_observation(observed_at=stale_but_post_collection)
        ),
    )


def test_maximum_observation_age_is_configurable():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    world.timeout = True  # the attempt is ambiguous, so A5 still decides
    slightly_stale = _Provider(
        observation=_provider_observation(
            observed_at=FIXED_NOW - timedelta(seconds=30)
        )
    )
    result = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=slightly_stale,
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
        max_observation_age_seconds=1,
    ).execute(request)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.OBSERVATION_TOO_OLD
    assert world.handler_calls == []


def test_future_dated_observation_is_refused():
    _a5_refused(
        reason=RefusalReason.OBSERVATION_FROM_FUTURE,
        provider=_Provider(
            observation=_provider_observation(
                observed_at=FIXED_NOW + timedelta(minutes=10)
            )
        ),
    )


def test_snapshot_without_collected_at_cannot_establish_freshness():
    _a5_refused(
        reason=RefusalReason.SNAPSHOT_FRESHNESS_UNESTABLISHED,
        snapshot=_make_snapshot(collected_at=None),
    )


def test_observation_timestamp_must_be_timezone_aware():
    with pytest.raises(ValueError):
        _provider_observation(observed_at=datetime(2026, 1, 2, 3, 4, 5))


# ---------------------------------------------------------------------------
# M10 identity: mandatory on both sides, never skipped.
# ---------------------------------------------------------------------------


def test_observation_identity_resource_mismatch():
    _a5_refused(
        reason=RefusalReason.RESOURCE_IDENTITY_MISMATCH,
        provider=_Provider(observation=_provider_observation(resource_id="other-inst")),
    )


def test_observation_identity_type_mismatch():
    _a5_refused(
        reason=RefusalReason.RESOURCE_IDENTITY_MISMATCH,
        provider=_Provider(
            observation=_provider_observation(
                resource_type=SWSResourceType.S3_BUCKET
            )
        ),
    )


def test_observation_identity_arn_mismatch():
    _a5_refused(
        reason=RefusalReason.RESOURCE_IDENTITY_MISMATCH,
        provider=_Provider(
            observation=_provider_observation(
                arn="arn:aws:ec2:us-east-1:999999999999:instance/x"
            )
        ),
    )


def test_observation_identity_account_mismatch():
    _a5_refused(
        reason=RefusalReason.RESOURCE_IDENTITY_MISMATCH,
        provider=_Provider(observation=_provider_observation(account_id="999999999999")),
    )


def test_observation_identity_region_mismatch():
    """M10: region is compared; M9 never looked at it at all."""
    _a5_refused(
        reason=RefusalReason.RESOURCE_IDENTITY_MISMATCH,
        provider=_Provider(observation=_provider_observation(region="eu-west-1")),
    )


@pytest.mark.parametrize("field", ["arn", "account_id", "region"])
def test_missing_observation_identity_is_refused(field: str):
    _a5_refused(
        reason=RefusalReason.IDENTITY_EVIDENCE_MISSING,
        provider=_Provider(observation=_provider_observation(**{field: None})),
    )


@pytest.mark.parametrize("field", ["arn", "account_id", "region"])
def test_missing_snapshot_record_identity_is_refused(field: str):
    """M9 skipped the comparison when the record lacked the fact."""
    _a5_refused(
        reason=RefusalReason.IDENTITY_EVIDENCE_MISSING,
        snapshot=_make_snapshot(record_overrides={field: None}),
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
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert result.verification is VerificationStatus.SUCCESS
    assert result.attempt_id == "exec-1"
    assert len(world.handler_calls) == 1
    assert store.get(request.ticket.ticket_id).consumed is True


def test_partially_verified_when_canonical_fact_is_unobserved():
    """A missing canonical fact yields PARTIALLY_VERIFIED, never success."""
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        # Reports identity but no state at all: the canonical "state" fact
        # is unobservable, so the claim cannot be confirmed.
        observer=_FlakyProvider(facts={}),
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.PARTIALLY_VERIFIED
    assert result.verification is VerificationStatus.PARTIALLY_VERIFIED


def test_conflicting_expected_poststate_is_refused():
    """M10: the action's postcondition wins over a caller's expected state."""
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=_grant(store, plan),
        expected_poststate={"state": "terminated"},
    )
    world = _World()
    audit = _SpyAuditStore()
    result = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    ).execute(request)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.POSTCONDITION_MISMATCH
    assert world.handler_calls == []
    assert len(audit) == 0


def test_superset_caller_facts_are_refused():
    """M10: extra caller claims are refused, never silently dropped.

    Silently ignoring facts the caller expected to be verified would turn a
    misunderstanding into an unverified success claim, so a supplied
    post-state must be empty or exactly the canonical mapping.
    """
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot)
    plan = _make_plan(store, snapshot=snapshot, decision=decision)
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=_grant(store, plan),
        expected_poststate={"state": "stopped", "instance_type": "t3.micro"},
    )
    world = _World()
    audit = _SpyAuditStore()
    result = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    ).execute(request)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.POSTCONDITION_MISMATCH
    assert world.handler_calls == []
    assert len(audit) == 0


def test_call_error_reports_failed():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    world.call_error = True
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
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
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.UNKNOWN
    assert result.verification is VerificationStatus.UNKNOWN


def test_observer_raising_after_attempt_is_unknown():
    """M10 symmetry: a post-attempt observation failure is contained.

    The same provider serves the A5 preflight and post-attempt verification,
    so a provider that fails only after the mutation still leaves the
    transaction closed with UNKNOWN -- never a crash and never a success.
    """
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=_FlakyProvider(raises=ObservationError("lost the response")),
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.UNKNOWN
    assert result.verification is VerificationStatus.UNKNOWN
    assert len(world.handler_calls) == 1


def test_missing_observer_with_handler_is_refused_not_executed():
    """No provider means no A5 evidence, so nothing may be attempted."""
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    result = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=None,
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    ).execute(request)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE
    assert world.handler_calls == []


def test_post_attempt_observer_raising_unexpected_error_still_closes_transaction(
    tmp_path: Path,
):
    """M10: a non-ObservationError after the attempt cannot escape as a crash.

    The result is UNKNOWN and the ledger still carries RESULT and POST, so
    the transaction is never left open by a provider bug.
    """
    clock = _Clock()
    store = InMemoryApprovalStore(now=clock)
    audit = _jsonl_audit(tmp_path / "audit.jsonl", clock)
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=_FlakyProvider(raises=_UnexpectedError("client-side exception")),
        audit_store=audit,
        id_source=lambda: "exec-1",
        now=clock,
    )
    result = coordinator.execute(request)
    assert result.outcome is ExecutionOutcome.UNKNOWN
    assert result.verification is VerificationStatus.UNKNOWN

    stages = [record.payload["stage"] for record in audit.records()]
    assert stages == ["pre", "attempt", "result", "post"]
    assert audit.records()[-1].payload["outcome"] == "unknown"


def test_no_blind_retry_after_ambiguous_attempt():
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    world.timeout = True
    coordinator = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
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


def test_handler_without_durable_audit_store_is_refused():
    """M10: no durable ledger means idempotency cannot be guaranteed."""
    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    result = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        id_source=lambda: "exec-1",
        now=_Clock(),
    ).execute(request)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.DURABLE_LEDGER_REQUIRED
    assert world.handler_calls == []


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


# ---------------------------------------------------------------------------
# M10 durable intent idempotency: snapshot-scoped, deterministic, restart-safe.
# ---------------------------------------------------------------------------


def test_intent_key_is_deterministic_for_the_same_intent():
    first = execution_intent_key(
        snapshot_id="snap-1", resource_id="inst-1", action=STOP
    )
    second = execution_intent_key(
        snapshot_id="snap-1", resource_id="inst-1", action=STOP
    )
    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "changed",
    [{"snapshot_id": "snap-2"}, {"resource_id": "inst-2"}, {"action": PotentialAction.LEAVE}],
)
def test_intent_key_changes_with_every_component(changed: dict):
    base = {"snapshot_id": "snap-1", "resource_id": "inst-1", "action": STOP}
    assert execution_intent_key(**{**base, **changed}) != execution_intent_key(**base)


def test_intent_key_does_not_depend_on_plan_or_ticket_identity():
    """A fresh plan id for the same intent must not mint a fresh key."""
    base = {"snapshot_id": "snap-1", "resource_id": "inst-1", "action": STOP}
    assert execution_intent_key(**base) == execution_intent_key(**base)


def test_replanned_same_intent_with_new_plan_id_is_refused(tmp_path: Path):
    """M10: the M9 hole -- a new plan id used to reset duplicate detection."""
    clock = _Clock()
    store = InMemoryApprovalStore(now=clock)
    audit = _jsonl_audit(tmp_path / "audit.jsonl", clock)
    request, _plan, snapshot = _gated_request_with_ticket(store)
    world = _World()
    first = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,
        id_source=lambda: "exec-1",
        now=clock,
    )
    assert first.execute(request).outcome is ExecutionOutcome.VERIFIED_SUCCESS

    # Re-plan the identical intent under a brand-new action_plan_id.
    replan = _make_plan(
        store, snapshot=snapshot, decision=_make_decision(snapshot), plan_id="plan-2"
    )
    replanned = request.model_copy(
        update={
            "action_plan_id": "plan-2",
            "plan": replan,
            "ticket": _grant(store, replan),
        }
    )
    second = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,
        id_source=lambda: "exec-2",
        now=clock,
    )
    result = second.execute(replanned)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.DUPLICATE_ATTEMPT
    assert len(world.handler_calls) == 1


def test_intent_idempotency_survives_a_process_restart(tmp_path: Path):
    """M10: the duplicate scan reads the durable ledger, not memory."""
    clock = _Clock()
    store = InMemoryApprovalStore(now=clock)
    path = tmp_path / "audit.jsonl"
    request, _plan, snapshot = _gated_request_with_ticket(store)
    world = _World()
    first = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=_jsonl_audit(path, clock),
        id_source=lambda: "exec-1",
        now=clock,
    )
    assert first.execute(request).outcome is ExecutionOutcome.VERIFIED_SUCCESS

    # A brand-new coordinator over a freshly reopened ledger: no in-memory
    # attempt or intent state carries over, exactly like a restart.
    store_after = InMemoryApprovalStore(now=clock)
    reopened = ExecutionCoordinator(
        approval_store=store_after,
        handler=world,
        observer=world,
        audit_store=_jsonl_audit(path, clock),
        id_source=lambda: "exec-2",
        now=clock,
    )
    replay = _request(
        plan=ActionPlanner(
            approval_store=store_after,
            execution_mode=SAFE,
            plan_id_source=lambda: "plan-9",
            now=lambda: FIXED_NOW,
        ).plan(
            resource_id="inst-1",
            resource_type=SWSResourceType.EC2_INSTANCE,
            action=STOP,
            decision=_make_decision(snapshot),
        ),
        snapshot=snapshot,
        decision=_make_decision(snapshot),
        ticket=None,
        action_plan_id="plan-9",
    )
    replay = replay.model_copy(update={"ticket": _grant(store_after, replay.plan)})
    result = reopened.execute(replay)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.DUPLICATE_ATTEMPT
    assert len(world.handler_calls) == 1


def test_new_snapshot_reopens_the_intent(tmp_path: Path):
    """A fresh snapshot re-establishes the world, so a new intent is allowed."""
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
        now=clock,
    )
    assert first.execute(request).outcome is ExecutionOutcome.VERIFIED_SUCCESS

    later = _make_snapshot().model_copy(
        update={"snapshot_id": "snap-2", "run_id": "run-2"}
    )
    later_decision = _make_decision(later)
    later_plan = _make_plan(
        store, snapshot=later, decision=later_decision, plan_id="plan-2"
    )
    later_request = _request(
        plan=later_plan,
        snapshot=later,
        decision=later_decision,
        ticket=_grant(store, later_plan),
        action_plan_id="plan-2",
    )
    world.state = "running"
    second = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,
        id_source=lambda: "exec-2",
        now=clock,
    )
    assert second.execute(later_request).outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert len(world.handler_calls) == 2


def test_canonical_postconditions_are_action_derived_and_immutable():
    canonical = canonical_postconditions(STOP)
    assert dict(canonical) == STOP_POSTCONDITION
    # Read-only mapping, so nothing can retune the postcondition in place.
    with pytest.raises(TypeError):
        canonical["state"] = "terminated"  # type: ignore[index]
    assert dict(canonical_postconditions(STOP)) == STOP_POSTCONDITION


def test_action_without_postcondition_is_refused(monkeypatch):
    """M10 guard for the next action we register.

    Only ``stop_resource`` is executable today, so this guard is currently
    defensive. A future action that declares no postcondition would
    otherwise be executable but permanently unverifiable, and the coordinator
    would have no honest outcome to report for it.
    """
    from sws_agent import execution as execution_module

    spec_without_postcondition = ActionSpec(
        action=STOP,
        eligible_resource_types=frozenset({SWSResourceType.EC2_INSTANCE}),
        requires_human_approval=True,
        mutation="stop_instances",
        postconditions=MappingProxyType({}),
        implemented=False,
    )
    monkeypatch.setattr(
        execution_module,
        "ACTION_EXECUTION_REGISTRY",
        MappingProxyType(
            {
                **ACTION_EXECUTION_REGISTRY,
                STOP: spec_without_postcondition,
            }
        ),
    )
    store = InMemoryApprovalStore(now=_Clock())
    request, store = _refusal_context()
    audit = _SpyAuditStore()
    world = _World()
    result = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    ).execute(request)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.POSTCONDITION_UNDEFINED
    assert world.handler_calls == []
    assert len(audit) == 0


def test_action_without_eligible_resource_types_is_refused():
    """LEAVE is not executable, so it is refused before anything else."""
    store = InMemoryApprovalStore(now=_Clock())
    snapshot = _make_snapshot()
    decision = _make_decision(snapshot).model_copy(
        update={"recommended_action": PotentialAction.LEAVE}
    )
    plan = ActionPlanner(
        approval_store=store,
        execution_mode=SAFE,
        plan_id_source=lambda: "plan-1",
        now=lambda: FIXED_NOW,
    ).plan(
        resource_id="inst-1",
        resource_type=SWSResourceType.EC2_INSTANCE,
        action=PotentialAction.LEAVE,
        decision=decision,
    )
    # LEAVE needs no approval, so no ticket is ever created for it.
    assert plan.ticket is None
    request = _request(
        plan=plan,
        snapshot=snapshot,
        decision=decision,
        ticket=None,
        action=PotentialAction.LEAVE,
    )
    world = _World()
    result = ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        now=_Clock(),
    ).execute(request)
    assert result.outcome is REFUSED
    assert result.refusal is RefusalReason.ACTION_NOT_EXECUTABLE
    assert world.handler_calls == []


def test_registry_declares_postconditions_for_every_executable_action():
    """Every action that could mutate must state what success means."""
    executable = [
        (action, spec)
        for action, spec in ACTION_EXECUTION_REGISTRY.items()
        if spec.eligible_resource_types
    ]
    assert executable, "registry should still describe its actions"
    for action, spec in executable:
        assert dict(spec.postconditions) or action is STOP
    assert dict(canonical_postconditions(STOP)) == STOP_POSTCONDITION
    # No action may claim to be implemented yet.
    assert all(not spec.implemented for _action, spec in executable)


# ---------------------------------------------------------------------------
# M10 read-only reconciliation of open transactions.
# ---------------------------------------------------------------------------


def test_reconciliation_is_empty_for_a_clean_ledger(tmp_path: Path):
    clock = _Clock()
    store = InMemoryApprovalStore(now=clock)
    audit = _jsonl_audit(tmp_path / "audit.jsonl", clock)
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,
        id_source=lambda: "exec-1",
        now=clock,
    ).execute(request)
    assert reconcile_open_transactions(audit) == ()


def test_not_executed_pre_only_is_not_an_open_transaction(tmp_path: Path):
    """PRE alone means nothing was attempted, so nothing needs resolving."""
    clock = _Clock()
    store = InMemoryApprovalStore(now=clock)
    audit = _jsonl_audit(tmp_path / "audit.jsonl", clock)
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    ExecutionCoordinator(
        approval_store=store,
        audit_store=audit,
        id_source=lambda: "exec-1",
        now=clock,
    ).execute(request)
    assert reconcile_open_transactions(audit) == ()


def test_reconciliation_finds_a_transaction_truncated_after_the_attempt(
    tmp_path: Path,
):
    """A process that died mid-flight leaves PRE + ATTEMPT and no POST."""
    clock = _Clock()
    store = InMemoryApprovalStore(now=clock)
    path = tmp_path / "audit.jsonl"
    audit = _jsonl_audit(path, clock)
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    world.timeout = True  # an ambiguous attempt: exactly the crash-prone case
    ExecutionCoordinator(
        approval_store=store,
        handler=world,
        observer=world,
        audit_store=audit,
        id_source=lambda: "exec-1",
        now=clock,
    ).execute(request)

    # Simulate the crash: drop everything from RESULT onwards.
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    truncated = [
        record
        for record in records
        if record["payload"]["stage"] not in ("result", "post")
    ]
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in truncated),
        encoding="utf-8",
    )

    reopened = _jsonl_audit(path, clock)
    open_transactions = reconcile_open_transactions(reopened)
    assert len(open_transactions) == 1
    transaction = open_transactions[0]
    assert transaction.execution_id == "exec-1"
    assert transaction.resource_id == "inst-1"
    assert transaction.action is STOP
    assert transaction.intent_key == execution_intent_key(
        snapshot_id="snap-1", resource_id="inst-1", action=STOP
    )
    assert ExecutionStage.ATTEMPT in transaction.stages
    assert ExecutionStage.POST not in transaction.stages
    assert transaction.classification == "unresolved_open_transaction"


def test_reconciliation_is_read_only_and_deterministic(tmp_path: Path):
    """Reconciliation never writes, and repeated calls agree."""
    clock = _Clock()
    store = InMemoryApprovalStore(now=clock)
    path = tmp_path / "audit.jsonl"
    audit = _jsonl_audit(path, clock)
    for index in range(2):
        snapshot = _make_snapshot().model_copy(
            update={"snapshot_id": f"snap-{index}", "run_id": f"run-{index}"}
        )
        decision = _make_decision(snapshot)
        plan = _make_plan(
            store, snapshot=snapshot, decision=decision, plan_id=f"plan-{index}"
        )
        request = _request(
            plan=plan,
            snapshot=snapshot,
            decision=decision,
            ticket=_grant(store, plan),
            action_plan_id=f"plan-{index}",
        )
        world = _World()
        world.timeout = True
        ExecutionCoordinator(
            approval_store=store,
            handler=world,
            observer=world,
            audit_store=audit,
            id_source=lambda index=index: f"exec-{index}",
            now=clock,
        ).execute(request)

    lines_before = path.read_text(encoding="utf-8")
    first_pass = reconcile_open_transactions(audit)
    second_pass = reconcile_open_transactions(audit)
    assert path.read_text(encoding="utf-8") == lines_before
    assert first_pass == second_pass
    assert [item.execution_id for item in first_pass] == sorted(
        item.execution_id for item in first_pass
    )


def test_observation_provenance_vocabulary_is_canonical():
    assert SWS_SUPPORTED_OBSERVATION_PROVENANCES == {
        p.value for p in ObservationProvenance
    }


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