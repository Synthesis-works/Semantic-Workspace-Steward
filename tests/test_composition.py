"""M14-A: the dry-run composition, assembled for real and proved inert.

Every component asserted here is a production class against a real file:
:class:`DurableApprovalStore`, :class:`DurableExecutionLedger`,
:class:`Ec2InstanceObservationProvider`, and
:class:`ExecutionCoordinator`. The only substituted piece is the AWS seam, and
:class:`ReadOnlyEc2Seam` exists precisely so the substitution is narrow and
checked rather than convenient.

The M13 investigation found 4 of 4 workers crossing a boundary that only 1 of 4
had approval for, and 169 of 720 audit records silently lost. The tests below
are the two regressions for those findings, plus the composition that makes
them meaningful: a coordinator that is actually wired can only ever be handed a
handler that cannot reach AWS.

Deliberately absent: any mutating handler, any real AWS call, any credential,
any network. ``ec2:StopInstances`` remains unimplemented and out of scope.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from sws_agent.composition import (
    APPROVAL_LEDGER_FILENAME,
    EXECUTION_LEDGER_FILENAME,
    READ_ONLY_EC2_METHODS,
    DryRunComposition,
    MutatingSeamRequired,
    NonMutatingHandlerRequired,
    NullMutationHandler,
    ReadOnlyEc2Seam,
    RecordingMutationHandler,
    build_dry_run_composition,
    cast_mutation_handler,
)
from sws_agent.constants import (
    DispatchDisposition,
    ExecutionMode,
    ExecutionOutcome,
    PotentialAction,
    RefusalReason,
    RiskLevel,
    SWSResourceType,
)
from sws_agent.ec2_observation import Ec2InstanceObservationProvider
from sws_agent.execution import (
    ACTION_EXECUTION_REGISTRY,
    ExecutionCoordinator,
    execution_intent_key,
)
from sws_agent.execution_ledger import (
    DurableExecutionLedger,
    ExecutionReservationState,
    ReexecutionClass,
)
from sws_agent.mcp.server import SwsMcpServer
from sws_agent.models import (
    ActionPlan,
    DispatchEvidence,
    ExecutionRequest,
    PolicyDecision,
    ResourceRecord,
    WorkspaceSnapshot,
)
from sws_agent.workflow import ActionPlanner

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
REGION = "us-east-1"
ACCOUNT = "123456789012"
INSTANCE_ID = "i-0abc123def4567890"
ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE_ID}"

SNAPSHOT_COLLECTED = FIXED_NOW - timedelta(minutes=30)
STOP = PotentialAction.STOP_RESOURCE


class FakeClock:
    """Frozen, timezone-aware clock so every assertion is deterministic."""

    def __init__(self, current: datetime = FIXED_NOW) -> None:
        self._current = current

    def __call__(self) -> datetime:
        return self._current


class FakePaginator:
    def __init__(self, pages: list[Any]) -> None:
        self._pages = pages

    def paginate(self, **kwargs: Any) -> Any:
        del kwargs
        return iter(self._pages)


class FakeEc2Seam:
    """Read-only stand-in exposing exactly the one permitted method."""

    def __init__(self, state: str = "running") -> None:
        self.state = state
        self.describe_instances_calls = 0

    def describe_instances(self) -> FakePaginator:
        self.describe_instances_calls += 1
        return FakePaginator(
            [
                {
                    "Reservations": [
                        {
                            "OwnerId": ACCOUNT,
                            "Instances": [
                                {
                                    "InstanceId": INSTANCE_ID,
                                    "State": {"Code": 16, "Name": self.state},
                                }
                            ],
                        }
                    ]
                }
            ]
        )


class WideEc2Seam(FakeEc2Seam):
    """A seam with mutation capability -- must be refused."""

    def stop_instances(self, **kwargs: Any) -> Any:  # pragma: no cover - refused
        raise AssertionError("a dry run must never reach ec2:StopInstances")


def _snapshot() -> WorkspaceSnapshot:
    record = ResourceRecord(
        resource_id=INSTANCE_ID,
        resource_type=SWSResourceType.EC2_INSTANCE,
        name="instance-one",
        arn=ARN,
        account_id=ACCOUNT,
        region=REGION,
    )
    return WorkspaceSnapshot(
        snapshot_id="snap-1",
        run_id="run-1",
        created_at=FIXED_NOW - timedelta(minutes=60),
        collected_at=SNAPSHOT_COLLECTED,
        regions=[REGION],
        resource_types=[SWSResourceType.EC2_INSTANCE],
        resources=[record],
        counts={SWSResourceType.EC2_INSTANCE: 1},
    )


def _decision(snapshot: WorkspaceSnapshot) -> PolicyDecision:
    return PolicyDecision(
        resource_id=INSTANCE_ID,
        recommended_action=STOP,
        risk_level=RiskLevel.MEDIUM,
        needs_approval=True,
        decision_id="dec-1",
        snapshot_id=snapshot.snapshot_id,
        run_id=snapshot.run_id,
    )


def _composition(tmp_path: Path, **kwargs: Any) -> DryRunComposition:
    kwargs.setdefault("now", FakeClock())
    kwargs.setdefault("ec2_client", FakeEc2Seam())
    kwargs.setdefault("region", REGION)
    return build_dry_run_composition(ledger_dir=tmp_path / "ledgers", **kwargs)


def _prepare(composition: DryRunComposition) -> tuple[ActionPlan, ExecutionRequest]:
    """Build a fully gated request: plan, decision, snapshot, granted ticket."""
    snapshot = _snapshot()
    decision = _decision(snapshot)
    plan = ActionPlanner(
        approval_store=composition.approval_store,
        execution_mode=ExecutionMode.SAFE,
        plan_id_source=lambda: "plan-1",
        now=lambda: FIXED_NOW,
    ).plan(
        resource_id=INSTANCE_ID,
        resource_type=SWSResourceType.EC2_INSTANCE,
        action=STOP,
        decision=decision,
    )
    granted = composition.approval_store.grant(plan.ticket.ticket_id, decided_by="human-1")
    request = ExecutionRequest(
        action_plan_id=plan.action_plan_id,
        resource_id=INSTANCE_ID,
        action=STOP,
        execution_mode=plan.execution_mode,
        plan=plan,
        decision=decision,
        snapshot=snapshot,
        ticket=granted,
    )
    return plan, request


# ---------------------------------------------------------------------------
# The composition really is the production classes
# ---------------------------------------------------------------------------


def test_composition_builds_the_real_production_classes(tmp_path):
    composition = _composition(tmp_path)
    try:
        assert isinstance(composition.coordinator, ExecutionCoordinator)
        assert isinstance(composition.execution_ledger, DurableExecutionLedger)
        assert isinstance(composition.observer, Ec2InstanceObservationProvider)
        assert composition.approval_store.__class__.__name__ == "DurableApprovalStore"
        assert isinstance(composition.handler, RecordingMutationHandler)
        # Every store is a real file, not an in-memory stand-in.
        assert composition.execution_ledger.path.exists()
        assert composition.approval_store.path.exists()
        assert composition.audit_store is not None
        assert composition.audit_store.path.exists()
    finally:
        composition.close()


def test_composition_ledgers_live_in_the_requested_directory(tmp_path):
    root = tmp_path / "ledgers"
    composition = _composition(tmp_path)
    try:
        assert composition.approval_store.path == root / APPROVAL_LEDGER_FILENAME
        assert composition.execution_ledger.path == root / EXECUTION_LEDGER_FILENAME
    finally:
        composition.close()


def test_composition_survives_reopening_the_same_directory(tmp_path):
    """Durable means durable: a second composition sees the first's state."""
    first = _composition(tmp_path)
    _, request = _prepare(first)
    first_result = first.coordinator.execute(request)
    first.close()

    second = _composition(tmp_path)
    try:
        first_path = tmp_path / "ledgers" / EXECUTION_LEDGER_FILENAME
        assert second.execution_ledger.path == first_path
        assert first_path.exists()
        executions = second.execution_ledger.all_executions()
        assert len(executions) == 1
        assert executions[0].intent_key == execution_intent_key(
            snapshot_id="snap-1", resource_id=INSTANCE_ID, action=STOP
        )
        assert first_result.outcome is ExecutionOutcome.NOT_EXECUTED
    finally:
        second.close()


# ---------------------------------------------------------------------------
# End-to-end: every ledger transition, deterministically
# ---------------------------------------------------------------------------


def test_dry_run_crosses_the_real_coordinator_path_end_to_end(tmp_path):
    """gate -> reserve -> consume -> mark_attempted -> handler -> verify -> record.

    The reservation's ``revision`` is the evidence that all three transitions
    happened. The transition table has no ``RESERVED -> RESOLVED`` edge, so
    reaching ``RESOLVED`` at revision 2 means the execution passed through
    ``ATTEMPTED`` -- the boundary was recorded as crossed.
    """
    composition = _composition(tmp_path)
    try:
        _plan, request = _prepare(composition)
        intent = execution_intent_key(
            snapshot_id="snap-1", resource_id=INSTANCE_ID, action=STOP
        )
        assert composition.execution_ledger.all_executions() == ()

        result = composition.coordinator.execute(request)

        # The gate passed and the boundary was crossed exactly once.
        assert result.outcome is ExecutionOutcome.NOT_EXECUTED
        assert result.refusal is None
        assert len(composition.handler) == 1
        assert composition.handler.crossings()[0].resource_id == INSTANCE_ID

        # RESERVED -> ATTEMPTED -> RESOLVED, and nothing skipped.
        reservations = composition.execution_ledger.all_executions()
        assert len(reservations) == 1
        reservation = reservations[0]
        assert reservation.intent_key == intent
        assert reservation.state is ExecutionReservationState.RESOLVED
        assert reservation.revision == 2
        assert reservation.outcome is ExecutionOutcome.NOT_EXECUTED
        assert reservation.may_have_crossed_boundary is True
        assert reservation.is_terminal is True
        assert reservation.action is STOP
        # M15-C: the basis is NO_EFFECT because the boundary positively
        # reported NOT_DISPATCHED. The structural non-mutation property is now
        # recorded as a fact rather than inferred from a failed postcondition.
        assert reservation.reexecution_class is ReexecutionClass.NO_EFFECT
        assert reservation.permits_reexecution is True

        # Approval was consumed before the effect, not after.
        ticket_id = request.ticket.ticket_id
        assert composition.approval_store.get(ticket_id).status.value == "consumed"

        # The ledger's own integrity and lineage checks pass.
        composition.execution_ledger.verify()
        composition.execution_ledger.verify_lineage()
        composition.approval_store.verify()

        # Audit evidence exists, and is evidence only: no execution decision
        # was taken from it (that is the execution ledger's job).
        kinds = {record.kind for record in composition.audit_store.records()}
        assert "execution" in {kind.value for kind in kinds}
    finally:
        composition.close()


def test_dry_run_never_reports_success_because_it_changes_nothing(tmp_path):
    """A run that dispatched nothing must not claim the instance was stopped.

    The canonical postcondition is ``state == "stopped"``. The dry run's handler
    performs no mutation and reports ``NOT_DISPATCHED``, so the coordinator
    never verifies at all. Reporting ``VERIFIED_SUCCESS`` here would be the
    exact fabrication this milestone exists to prevent.

    M15-C makes this assertion stronger rather than merely changing it. The
    previous behaviour ran verification, observed the still-running instance,
    and inferred ``FAILED`` -- a correct verdict reached by verifying a
    mutation that was never sent. The outcome is now ``NOT_EXECUTED``, which
    asserts the stronger and more specific truth that no mutation happened,
    and the recorded basis is ``NO_EFFECT`` so the retry is authorized on a
    positive statement rather than on an unverified inference.
    """
    composition = _composition(tmp_path)
    try:
        _plan, request = _prepare(composition)
        result = composition.coordinator.execute(request)
        assert result.outcome is not ExecutionOutcome.VERIFIED_SUCCESS
        assert result.outcome is ExecutionOutcome.NOT_EXECUTED
        reservation = composition.execution_ledger.all_executions()[0]
        assert reservation.outcome is not ExecutionOutcome.UNKNOWN
        assert reservation.state is ExecutionReservationState.RESOLVED
        assert reservation.reexecution_class is ReexecutionClass.NO_EFFECT
    finally:
        composition.close()


def test_dry_run_records_evidence_that_nothing_was_dispatched(tmp_path):
    composition = _composition(tmp_path)
    try:
        _plan, request = _prepare(composition)
        composition.coordinator.execute(request)
        execution_records = [
            record
            for record in composition.audit_store.records()
            if record.kind.value == "execution"
        ]
        assert execution_records
        payloads = [record.payload for record in execution_records]
        assert any(payload.get("outcome") for payload in payloads)
        # No record anywhere may claim an EC2 mutation was issued.
        for record in composition.audit_store.records():
            assert "StopInstances" not in json.dumps(record.payload)
    finally:
        composition.close()


def test_composition_without_audit_refuses_to_cross_the_boundary(tmp_path):
    """No durable record of the crossing means the crossing does not happen.

    The coordinator checks the audit store and the execution ledger
    independently, and refuses either way. This is the fail-closed property
    that keeps a missing evidence trail from becoming a silent execution.
    """
    composition = _composition(tmp_path, audit=False)
    try:
        assert composition.audit_store is None
        assert composition.execution_ledger is not None
        _plan, request = _prepare(composition)
        result = composition.coordinator.execute(request)
        assert result.refusal is RefusalReason.DURABLE_LEDGER_REQUIRED
        assert result.outcome is ExecutionOutcome.REFUSED
        # Nothing was reserved, nothing consumed, nothing crossed.
        assert len(composition.handler) == 0
        assert composition.execution_ledger.all_executions() == ()
    finally:
        composition.close()


# ---------------------------------------------------------------------------
# M13 regression: 4 of 4 workers crossed where only 1 of 4 was approved
# ---------------------------------------------------------------------------

_WORKER_CHILD = """
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, {tests_dir!r})

from sws_agent.composition import build_dry_run_composition
from sws_agent.constants import ExecutionOutcome
from sws_agent.models import (
    ActionPlan, ExecutionRequest, PolicyDecision, WorkspaceSnapshot,
)

ledger_dir, worker_id, state_path, out_path = sys.argv[1:5]
FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
REGION = "us-east-1"
ACCOUNT = "123456789012"
INSTANCE_ID = "i-0abc123def4567890"


class Clock:
    def __call__(self):
        return FIXED_NOW


class Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages)


class Seam:
    def __init__(self, state):
        self._state = state

    def describe_instances(self):
        return Paginator(
            [{{
                "Reservations": [{{
                    "OwnerId": ACCOUNT,
                    "Instances": [{{
                        "InstanceId": INSTANCE_ID,
                        "State": {{"Code": 16, "Name": self._state}},
                    }}],
                }}],
            }}]
        )


state = json.loads(Path(state_path).read_text(encoding="utf-8"))
composition = build_dry_run_composition(
    ledger_dir=ledger_dir,
    ec2_client=Seam("running"),
    region=REGION,
    now=Clock(),
    worker_id=worker_id,
)
try:
    ticket = composition.approval_store.get(state["ticket_id"])
    plan = ActionPlan.model_validate(state["plan"])
    snapshot = WorkspaceSnapshot.model_validate(state["snapshot"])
    decision = PolicyDecision.model_validate(state["decision"])
    request = ExecutionRequest(
        action_plan_id=plan.action_plan_id,
        resource_id=INSTANCE_ID,
        action="stop_resource",
        execution_mode=plan.execution_mode,
        plan=plan,
        decision=decision,
        snapshot=snapshot,
        ticket=ticket,
    )
    result = composition.coordinator.execute(request)
    Path(out_path).write_text(
        json.dumps({{
            "worker_id": worker_id,
            "crossings": len(composition.handler),
            "outcome": result.outcome.value,
            "refusal": result.refusal.value if result.refusal else None,
        }}),
        encoding="utf-8",
    )
finally:
    composition.close()
"""


def test_two_independent_processes_on_one_intent_cross_once(tmp_path):
    """The M13 finding, as a regression: only one worker may cross.

    Two genuinely separate interpreters, each with its own coordinator, its own
    handler, and its own audit handle, contending for the same
    ``(intent_key, ticket_id)`` pair. Exactly one may reach the boundary.
    """
    import subprocess
    import sys

    root = tmp_path / "ledgers"
    composition = _composition(tmp_path)
    try:
        plan, request = _prepare(composition)
        state = {
            "ticket_id": plan.ticket.ticket_id,
            "plan": json.loads(plan.model_dump_json()),
            "snapshot": json.loads(request.snapshot.model_dump_json()),
            "decision": json.loads(request.decision.model_dump_json()),
        }
    finally:
        composition.close()

    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    child = _WORKER_CHILD.format(tests_dir=str(Path(__file__).parent))
    outs = []
    procs = []
    for worker in ("worker-a", "worker-b"):
        out_path = tmp_path / f"out-{worker}.json"
        outs.append(out_path)
        procs.append(
            subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [
                    sys.executable,
                    "-c",
                    child,
                    str(root),
                    worker,
                    str(state_path),
                    str(out_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    reports = []
    for proc, out_path in zip(procs, outs):
        _, stderr = proc.communicate(timeout=180)
        assert proc.returncode == 0, stderr.decode("utf-8", "replace")[-2000:]
        reports.append(json.loads(out_path.read_text(encoding="utf-8")))

    # Exactly one process crossed the boundary.
    assert sorted(r["crossings"] for r in reports) == [0, 1]
    # Exactly one executed; the loser was refused, never silently ignored. The
    # winner reports NOT_EXECUTED (M15-C): the null handler positively reported
    # NOT_DISPATCHED, so no mutation happened and none was claimed.
    outcomes = sorted(r["outcome"] for r in reports)
    assert outcomes.count("refused") == 1
    assert outcomes.count("not_executed") == 1
    loser = next(r for r in reports if r["crossings"] == 0)
    assert loser["refusal"] is not None

    # And the durable ledger agrees with the handlers.
    reopened = _composition(tmp_path)
    try:
        reservations = reopened.execution_ledger.all_executions()
        assert len(reservations) == 1
        assert reservations[0].state is ExecutionReservationState.RESOLVED
        assert reservations[0].may_have_crossed_boundary is True
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# The safety boundary: refusals, not conventions
# ---------------------------------------------------------------------------


def test_composition_refuses_a_handler_that_could_mutate(tmp_path):
    class MutatingHandler:
        def handle(self, request):  # pragma: no cover - never reached
            raise AssertionError("must never be constructed into a dry run")

    with pytest.raises(NonMutatingHandlerRequired):
        build_dry_run_composition(
            ledger_dir=tmp_path / "ledgers",
            ec2_client=FakeEc2Seam(),
            region=REGION,
            handler=MutatingHandler(),
        )
    # Nothing was constructed: the check runs before any store is opened.
    assert not (tmp_path / "ledgers" / EXECUTION_LEDGER_FILENAME).exists()


def test_composition_refuses_an_ec2_seam_with_mutation_capability(tmp_path):
    with pytest.raises(MutatingSeamRequired) as excinfo:
        build_dry_run_composition(
            ledger_dir=tmp_path / "ledgers",
            ec2_client=WideEc2Seam(),
            region=REGION,
        )
    assert "stop_instances" in str(excinfo.value)
    assert not (tmp_path / "ledgers" / EXECUTION_LEDGER_FILENAME).exists()


def test_read_only_seam_narrows_a_wider_client():
    wide = WideEc2Seam()
    narrow = ReadOnlyEc2Seam(wide)
    exposed = {name for name in dir(narrow) if not name.startswith("_")}
    assert exposed == set(READ_ONLY_EC2_METHODS)
    narrow.describe_instances()
    assert wide.describe_instances_calls == 1


def test_shipped_handlers_hold_no_way_to_reach_aws():
    for handler in (NullMutationHandler(), RecordingMutationHandler()):
        public = {name for name in vars(handler) if not name.startswith("_")}
        assert public <= {"_crossings"}
        assert not hasattr(handler, "client")
        assert not hasattr(handler, "session")
        assert not hasattr(handler, "credentials")
        evidence = handler.handle(
            ExecutionRequest(
                action_plan_id="plan-1",
                resource_id=INSTANCE_ID,
                action=STOP,
                execution_mode=ExecutionMode.SAFE,
            )
        )
        # M15-C: the shipped handlers report the truthful disposition, which is
        # that nothing was dispatched. They carry no AWS response fields, so
        # there is no error code, HTTP status, or exception class to carry.
        assert isinstance(evidence, DispatchEvidence)
        assert evidence.disposition is DispatchDisposition.NOT_DISPATCHED
        assert evidence.aws_error_code is None
        assert evidence.http_status is None
        assert evidence.exception_class is None
        assert evidence.sanitized["mutating"] is False


def test_composition_module_cannot_reach_aws():
    """The composition must not be able to reach AWS even by accident.

    Checked against the parsed AST rather than the raw source, because the
    module is *required* to name ``ec2:StopInstances`` in prose -- it is the
    milestone this composition defers. What must not exist is an import of a
    SDK, or code that names a mutating call.
    """
    import ast

    import sws_agent.composition as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Attribute):
            called.add(node.attr)
        elif isinstance(node, ast.Name):
            called.add(node.id)

    assert not {name for name in imported if "boto" in name}
    assert not {name for name in imported if "botocore" in name}
    assert not {name for name in called if "stop_instances" in name.lower()}
    assert not {name for name in called if "terminate_instances" in name.lower()}


def test_cast_mutation_handler_accepts_the_shipped_handlers():
    for handler in (NullMutationHandler(), RecordingMutationHandler()):
        assert cast_mutation_handler(handler) is handler


# ---------------------------------------------------------------------------
# Production stays inert
# ---------------------------------------------------------------------------


def test_production_registry_still_declares_nothing_implemented():
    for spec in ACTION_EXECUTION_REGISTRY.values():
        assert spec.implemented is False
    assert ACTION_EXECUTION_REGISTRY[STOP].implemented is False


def test_mcp_surface_is_unchanged_and_registers_no_handler():
    """Nine tools, and no path from the MCP server to a dry run."""
    from sws_agent.mcp.server import BUILTIN_TOOL_NAMES

    server = SwsMcpServer()
    assert len(BUILTIN_TOOL_NAMES) == 9
    assert server.tool_names() == sorted(BUILTIN_TOOL_NAMES)
    names = set(BUILTIN_TOOL_NAMES)
    assert "execute_action" not in names
    assert "apply_action" not in names
    assert "stop_resource" not in names

    import sws_agent.mcp.server as mcp_server

    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    assert "build_dry_run_composition" not in source
    assert "sws_agent.composition" not in source


def test_null_handler_returns_a_definite_non_mutating_attempt():
    """Crossing must be a definite no-op, not an ambiguous one.

    An ambiguous attempt would park the execution in ``UNRESOLVED``, which is
    the wrong terminal state for a boundary that was never really dispatched.

    M15-C replaces the ``ambiguous=False, call_error=False`` shape with the
    disposition ``NOT_DISPATCHED``. The previous shape could only express "no
    error and no ambiguity", which forced the coordinator to either run
    verification on a mutation that was never sent or call the execution
    unresolved. Naming the truth is strictly better: the coordinator skips
    verification and records a positive ``NO_EFFECT``.
    """
    handler = NullMutationHandler()
    evidence = handler.handle(
        ExecutionRequest(
            action_plan_id="plan-1",
            resource_id=INSTANCE_ID,
            action=STOP,
            execution_mode=ExecutionMode.SAFE,
        )
    )
    assert isinstance(evidence, DispatchEvidence)
    assert evidence.disposition is DispatchDisposition.NOT_DISPATCHED
    assert evidence.rejection_key is None
    assert evidence.sanitized["action"] == STOP.value
