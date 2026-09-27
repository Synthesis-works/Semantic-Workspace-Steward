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
    assert len(snapshot.resources) == 20
    assert snapshot.counts["s3_bucket"] == 12
    assert snapshot.counts["lambda_function"] == 8
    assert snapshot.partial is False
    assert snapshot.truncated is False
    first = [r.resource_id for r in snapshot.resources]
    assert first == sorted(first)
    assert len({r.resource_id for r in snapshot.resources}) == 20


def test_demo_account_is_none_everywhere() -> None:
    snapshot = build_demo_snapshot()
    assert DEMO_ACCOUNT_ID is None
    assert all(record.account_id is None for record in snapshot.resources)


def test_exactly_two_owned_none_resources_flagged_by_real_policy() -> None:
    snapshot = build_demo_snapshot()
    decisions = DemoBackend().evaluate_workspace(snapshot)
    flagged = [
        decision
        for decision in decisions
        if decision.recommended_action != PotentialAction.LEAVE
    ]
    assert len(flagged) == 2
    ids = {decision.resource_id for decision in flagged}
    assert ids == {"ghost-bucket-no-owner", "orphan-lambda-no-owner"}
    assert all(decision.confidence == 1.0 for decision in flagged)
    attributed = [
        decision
        for decision in decisions
        if decision.rule == POLICY_RULE_MISSING_OWNER_TAG
    ]
    assert len(attributed) == 2


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


def test_demo_approval_tickets_are_seeded_and_synthetic() -> None:
    backend = DemoBackend()
    tickets = backend.list_approvals()
    assert [t.ticket_id for t in tickets] == [
        "demo-ticket-0001",
        "demo-ticket-0002",
    ]
    assert all(t.action == PotentialAction.STOP_RESOURCE for t in tickets)
    assert all(t.status == ApprovalStatus.PENDING for t in tickets)
    assert all("nothing executes" in t.rationale.lower() for t in tickets)


def test_demo_grant_transitions_ticket_and_leaves_pending() -> None:
    backend = DemoBackend()
    ticket = backend.decide_ticket(
        "demo-ticket-0001", decision="grant", decided_by="demo-user"
    )
    assert ticket.status == ApprovalStatus.GRANTED
    remaining = backend.list_approvals()
    assert [t.ticket_id for t in remaining] == ["demo-ticket-0002"]


def test_demo_deny_transitions_ticket() -> None:
    backend = DemoBackend()
    ticket = backend.decide_ticket(
        "demo-ticket-0002", decision="deny", decided_by="demo-user"
    )
    assert ticket.status == ApprovalStatus.DENIED
    remaining = [t.ticket_id for t in backend.list_approvals()]
    assert remaining == ["demo-ticket-0001"]