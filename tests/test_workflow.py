"""M7 ActionPlanner: pre-execution decision -> authorization -> ticket.

These tests pin the workflow boundary: the planner must render
authorization deterministically, open PENDING tickets only when the gate
requires human approval, and never execute or touch AWS machinery.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sws_agent.approval import InMemoryApprovalStore
from sws_agent.authorization import ActionAuthorizer
from sws_agent.constants import (
    AuthorizationDecision,
    ExecutionMode,
    PotentialAction,
    SWSResourceType,
)
from sws_agent.models import ActionPlan
from sws_agent.workflow import ActionPlanner

_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "sws_agent" / "workflow.py"
)


@pytest.fixture
def store() -> InMemoryApprovalStore:
    return InMemoryApprovalStore()


def _planner(store: InMemoryApprovalStore, mode: ExecutionMode = ExecutionMode.SAFE):
    return ActionPlanner(approval_store=store, execution_mode=mode)


# 1. Zero-side-effect actions need no approval in any mode.
@pytest.mark.parametrize("action", [PotentialAction.LEAVE, PotentialAction.FLAG_FOR_REVIEW])
@pytest.mark.parametrize(
    "mode", [ExecutionMode.SAFE, ExecutionMode.REVIEW, ExecutionMode.AUTONOMOUS]
)
def test_zero_side_effect_actions_are_authorized_without_ticket(
    store: InMemoryApprovalStore, action: PotentialAction, mode: ExecutionMode
):
    plan = _planner(store, mode).plan(
        resource_id="b-1",
        resource_type=SWSResourceType.S3_BUCKET,
        action=action,
    )
    assert plan.authorization.decision is AuthorizationDecision.AUTHORIZED
    assert plan.authorization.requires_human_approval is False
    assert plan.ticket is None
    assert list(store.pending()) == []


# 2. Requesting approval opens a PENDING ticket carrying the rationale.
def test_request_approval_action_opens_pending_ticket(
    store: InMemoryApprovalStore,
):
    plan = _planner(store).plan(
        resource_id="b-1",
        resource_type=SWSResourceType.S3_BUCKET,
        action=PotentialAction.REQUEST_APPROVAL,
        rationale="candidate for review",
    )
    assert plan.authorization.decision is AuthorizationDecision.PENDING_APPROVAL
    assert plan.authorization.requires_human_approval is True
    assert plan.ticket is not None
    assert plan.ticket.status == "pending"
    assert plan.ticket.resource_id == "b-1"
    assert plan.ticket.action is PotentialAction.REQUEST_APPROVAL
    assert plan.ticket.rationale == "candidate for review"
    pending = list(store.pending())
    assert [t.ticket_id for t in pending] == [plan.ticket.ticket_id]


# 3. STOP_RESOURCE requires human approval in safe and review modes...
@pytest.mark.parametrize("mode", [ExecutionMode.SAFE, ExecutionMode.REVIEW])
def test_stop_resource_requires_human_approval_in_safe_and_review(
    store: InMemoryApprovalStore, mode: ExecutionMode
):
    plan = _planner(store, mode).plan(
        resource_id="fn-1",
        resource_type=SWSResourceType.LAMBDA_FUNCTION,
        action=PotentialAction.STOP_RESOURCE,
    )
    assert plan.authorization.decision is AuthorizationDecision.PENDING_APPROVAL
    assert plan.authorization.requires_human_approval is True
    assert plan.ticket is not None
    assert plan.ticket.status == "pending"


# 4. M10: ...but is BLOCKED in AUTONOMOUS mode -- no ticket, no authorization.
def test_stop_resource_blocked_in_autonomous_mode(
    store: InMemoryApprovalStore,
):
    plan = _planner(store, ExecutionMode.AUTONOMOUS).plan(
        resource_id="fn-1",
        resource_type=SWSResourceType.LAMBDA_FUNCTION,
        action=PotentialAction.STOP_RESOURCE,
    )
    assert plan.authorization.decision is AuthorizationDecision.BLOCKED
    assert plan.authorization.requires_human_approval is False
    assert plan.ticket is None
    assert list(store.pending()) == []


def test_planner_never_produces_an_authorized_destructive_plan(
    store: InMemoryApprovalStore,
):
    """M10: the plan can no longer self-assert authorization to mutate."""
    for mode in ExecutionMode:
        plan = _planner(store, mode).plan(
            resource_id="fn-1",
            resource_type=SWSResourceType.LAMBDA_FUNCTION,
            action=PotentialAction.STOP_RESOURCE,
        )
        assert plan.authorization.decision is not AuthorizationDecision.AUTHORIZED
        assert plan.executed is False


# 5. Nothing is ever executed: `executed` is always False across the matrix.
def test_plan_never_reports_execution(
    store: InMemoryApprovalStore,
):
    for mode in ExecutionMode:
        for action in PotentialAction:
            plan = _planner(store, mode).plan(
                resource_id="i-1",
                resource_type=SWSResourceType.EC2_INSTANCE,
                action=action,
            )
            assert plan.executed is False


# 6. Out-of-vocabulary actions fail fast at plan construction.
def test_invalid_action_fails_fast(store: InMemoryApprovalStore):
    with pytest.raises(ValueError):
        _planner(store).plan(
            resource_id="b-1",
            resource_type=SWSResourceType.S3_BUCKET,
            action="run_amok",
        )


# 7. The planner is deterministic: equal non-ticket inputs, equal plans.
def test_planner_is_deterministic_without_tickets(store: InMemoryApprovalStore):
    planner = _planner(store, ExecutionMode.SAFE)
    first = planner.plan(
        resource_id="b-1",
        resource_type=SWSResourceType.S3_BUCKET,
        action=PotentialAction.LEAVE,
    )
    second = planner.plan(
        resource_id="b-1",
        resource_type=SWSResourceType.S3_BUCKET,
        action=PotentialAction.LEAVE,
    )
    # M8: plans carry a unique identity + timestamp, so the determinism
    # contract covers everything except those two per-plan fields.
    assert first.model_dump(exclude={"action_plan_id", "created_at"}) == (
        second.model_dump(exclude={"action_plan_id", "created_at"})
    )
    assert first.action_plan_id != second.action_plan_id


# 8. ActionPlan round-trips through its JSON schema.
def test_plan_round_trips_through_model_schema(store: InMemoryApprovalStore):
    plan = _planner(store).plan(
        resource_id="fn-1",
        resource_type=SWSResourceType.LAMBDA_FUNCTION,
        action=PotentialAction.STOP_RESOURCE,
        rationale="runtime",
    )
    restored = ActionPlan.model_validate(plan.model_dump())
    assert restored.model_dump() == plan.model_dump()
    assert restored.executed is False


# 9. The default execution mode is SAFE and the injected authorizer is used.
def test_planner_defaults_and_authorizer_injection(store: InMemoryApprovalStore):
    planner = ActionPlanner(approval_store=store)
    assert planner.execution_mode is ExecutionMode.SAFE
    injected = ActionAuthorizer()
    assert ActionPlanner(
        approval_store=store, authorizer=injected
    )._authorizer is injected


# 10. M7 boundary source scan: the workflow module must never touch AWS machinery.
def test_workflow_module_has_no_aws_or_execution_reach():
    text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in ("boto3", "client(", "session", "executor"):
        assert forbidden not in text, forbidden


# 11. The workflow module only imports in-package primitives (no AWS SDK).
def test_workflow_module_imports_are_hermetic():
    source = _WORKFLOW_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert False, f"absolute import in workflow.py: {ast.unparse(node)}"
        if isinstance(node, ast.ImportFrom):
            relative = node.level > 0
            future = node.module == "__future__"
            assert relative or future, ast.unparse(node)