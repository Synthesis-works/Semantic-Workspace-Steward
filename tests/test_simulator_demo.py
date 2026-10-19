"""Hermetic tests for the synthetic demo backend.

No MCP server, no network, no AWS: this file exercises the deterministic
synthetic dataset and the DemoBackend's delegation to the REAL SWS core
(policy evaluation, relationship derivation, approval store).
"""

from __future__ import annotations

from sws_agent.constants import (
    POLICY_RULE_MISSING_OWNER_TAG,
    ApprovalStatus,
    PotentialAction,
    SWSResourceType,
)
from sws_agent.models import ClaimKind

from sws_agent.simulator.demo import (
    DEMO_ACCOUNT_ID,
    DemoBackend,
    build_demo_snapshot,
    demo_cost_estimates,
)


def test_demo_snapshot_is_deterministic() -> None:
    first = build_demo_snapshot()
    second = build_demo_snapshot()
    assert first == second
    assert first.snapshot_id.startswith("demo-")
    assert first.created_at == second.created_at


def test_demo_snapshot_shape_is_clear_and_synthetic() -> None:
    snapshot = build_demo_snapshot()
    assert len(snapshot.resources) == 23
    assert snapshot.counts["s3_bucket"] == 12
    assert snapshot.counts["lambda_function"] == 8
    assert snapshot.counts["ec2_instance"] == 3
    assert snapshot.partial is False
    assert snapshot.truncated is False
    first = [r.resource_id for r in snapshot.resources]
    assert first == sorted(first)
    assert len({r.resource_id for r in snapshot.resources}) == 23


def test_demo_account_is_none_everywhere() -> None:
    snapshot = build_demo_snapshot()
    assert DEMO_ACCOUNT_ID is None
    assert all(record.account_id is None for record in snapshot.resources)


def test_missing_owner_and_running_ec2_flagged_by_real_policy() -> None:
    snapshot = build_demo_snapshot()
    decisions = DemoBackend().evaluate_workspace(snapshot)
    flagged = [
        decision
        for decision in decisions
        if decision.recommended_action != PotentialAction.LEAVE
    ]
    # Two unattributed S3/Lambda resources flagged for review, plus the two
    # running EC2 instances the lifecycle rule recommends stopping.
    ids = {decision.resource_id for decision in flagged}
    assert ids == {
        "ghost-bucket-no-owner",
        "orphan-lambda-no-owner",
        "web-assets-api",
        "batch-worker-prod",
    }
    assert all(decision.confidence == 1.0 for decision in flagged)
    missing_owner = [
        decision
        for decision in decisions
        if decision.rule == POLICY_RULE_MISSING_OWNER_TAG
    ]
    assert len(missing_owner) == 2
    stop_eligible = [
        decision
        for decision in decisions
        if decision.recommended_action == PotentialAction.STOP_RESOURCE
    ]
    assert {d.resource_id for d in stop_eligible} == {
        "web-assets-api",
        "batch-worker-prod",
    }
    # The stopped instance is never recommended for a stop.
    stopped = next(
        decision
        for decision in decisions
        if decision.resource_id == "legacy-reporting-host"
    )
    assert stopped.recommended_action == PotentialAction.LEAVE


def test_demo_relationships_are_real_and_avoid_account_noise() -> None:
    backend = DemoBackend()
    snapshot = build_demo_snapshot()
    relationships = backend.derive_relationships(snapshot)
    assert relationships
    kinds = {relationship.relationship_type for relationship in relationships}
    assert "same_account" not in kinds
    assert "same_region" in kinds


def test_demo_cost_estimates_are_projected_and_never_savings() -> None:
    estimates = demo_cost_estimates()
    assert len(estimates) == 3
    assert all(estimate.projected for estimate in estimates)
    assert all(estimate.amount_usd > 0 for estimate in estimates)
    assert all(
        "savings" not in estimate.line_item.lower()
        for estimate in estimates
    )
    assert all(estimate.basis for estimate in estimates)


def test_demo_explanation_is_interpreted_and_demo_provided() -> None:
    backend = DemoBackend()
    snapshot = build_demo_snapshot()
    decision = next(
        decision
        for decision in backend.evaluate_workspace(snapshot)
        if decision.resource_id == "ghost-bucket-no-owner"
    )
    resource = next(
        record
        for record in snapshot.resources
        if record.resource_id == "ghost-bucket-no-owner"
    )
    explanation = backend.explain(resource, decision)
    assert explanation.provider == "demo"
    assert explanation.claim_kind == ClaimKind.INTERPRETED
    assert "synthetic" in explanation.reason.lower()


def test_demo_starts_with_no_pending_approval_tickets() -> None:
    # Tickets are only ever created through the real request_approval path,
    # never seeded, so an untouched demo has no pending approvals.
    assert DemoBackend().list_approvals() == []


def test_request_approval_opens_ticket_for_running_instance() -> None:
    backend = DemoBackend()
    plan = backend.request_approval(
        resource_id="web-assets-api",
        resource_type=SWSResourceType.EC2_INSTANCE,
        action=PotentialAction.STOP_RESOURCE,
        rationale="demo briefing",
    )
    assert plan.executed is False
    assert plan.authorization.requires_human_approval is True
    assert plan.ticket is not None
    assert plan.ticket.status == ApprovalStatus.PENDING
    assert plan.ticket.resource_id == "web-assets-api"
    tickets = backend.list_approvals()
    assert [t.ticket_id for t in tickets] == [plan.ticket.ticket_id]


def test_demo_grant_transitions_ticket_and_clears_pending() -> None:
    backend = DemoBackend()
    plan = backend.request_approval(
        resource_id="web-assets-api",
        resource_type=SWSResourceType.EC2_INSTANCE,
        action=PotentialAction.STOP_RESOURCE,
        rationale="demo briefing",
    )
    assert plan.ticket is not None
    ticket_id = plan.ticket.ticket_id
    ticket = backend.decide_ticket(
        ticket_id, decision="grant", decided_by="demo-user"
    )
    assert ticket.status == ApprovalStatus.GRANTED
    assert backend.list_approvals() == []


def test_demo_deny_transitions_ticket() -> None:
    backend = DemoBackend()
    plan = backend.request_approval(
        resource_id="batch-worker-prod",
        resource_type=SWSResourceType.EC2_INSTANCE,
        action=PotentialAction.STOP_RESOURCE,
        rationale="demo briefing",
    )
    assert plan.ticket is not None
    ticket_id = plan.ticket.ticket_id
    ticket = backend.decide_ticket(
        ticket_id, decision="deny", decided_by="demo-user"
    )
    assert ticket.status == ApprovalStatus.DENIED
    assert backend.list_approvals() == []