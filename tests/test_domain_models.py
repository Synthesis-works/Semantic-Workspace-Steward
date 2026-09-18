"""Domain models for approval tickets, explanations, and relationships.

Pins the new canonical enums (ClaimKind, ApprovalStatus), validates model
boundaries (non-empty required fields, sensible defaults, case-insensitive
normalization), and guards compatibility of the EvidenceBasis type
tightening on ResourceRelationship.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sws_agent.constants import (
    ApprovalStatus,
    ClaimKind,
    EvidenceBasis,
    PotentialAction,
)
from sws_agent.models import (
    ApprovalTicket,
    ExplanationResult,
    ResourceRelationship,
)

CREATED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_claim_kind_has_no_duplicate_values():
    values = [kind.value for kind in ClaimKind]
    assert len(values) == len(set(values)), "ClaimKind enum contains duplicate values"


def test_approval_status_has_no_duplicate_values():
    values = [status.value for status in ApprovalStatus]
    assert len(values) == len(set(values)), "ApprovalStatus enum contains duplicate values"


def test_approval_ticket_defaults_to_pending():
    ticket = ApprovalTicket(
        ticket_id="t1",
        resource_id="r1",
        action=PotentialAction.STOP_RESOURCE,
        created_at=CREATED_AT,
    )
    assert ticket.status is ApprovalStatus.PENDING
    assert ticket.rationale == ""
    assert ticket.decided_at is None
    assert ticket.decided_by == ""
    assert ticket.decision_reason == ""


def test_approval_ticket_normalizes_action_and_status():
    ticket = ApprovalTicket(
        ticket_id="t1",
        resource_id="r1",
        action="STOP_RESOURCE",
        status="GRANTED",
        created_at=CREATED_AT,
    )
    assert ticket.action is PotentialAction.STOP_RESOURCE
    assert ticket.status is ApprovalStatus.GRANTED


def test_approval_ticket_normalizes_lowercase_inputs():
    ticket = ApprovalTicket(
        ticket_id="t1",
        resource_id="r1",
        action="stop_resource",
        status="pending",
        created_at=CREATED_AT,
    )
    assert ticket.action is PotentialAction.STOP_RESOURCE
    assert ticket.status is ApprovalStatus.PENDING


def test_approval_ticket_rejects_empty_ticket_id():
    with pytest.raises(ValidationError):
        ApprovalTicket(
            ticket_id="",
            resource_id="r1",
            action=PotentialAction.STOP_RESOURCE,
            created_at=CREATED_AT,
        )


def test_approval_ticket_rejects_empty_resource_id():
    with pytest.raises(ValidationError):
        ApprovalTicket(
            ticket_id="t1",
            resource_id="",
            action=PotentialAction.STOP_RESOURCE,
            created_at=CREATED_AT,
        )


def test_approval_ticket_rejects_unknown_action():
    with pytest.raises(ValidationError):
        ApprovalTicket(
            ticket_id="t1",
            resource_id="r1",
            action="detonate",
            created_at=CREATED_AT,
        )


def test_explanation_result_defaults_to_interpreted():
    result = ExplanationResult(provider="null")
    assert result.text is None
    assert result.claim_kind is ClaimKind.INTERPRETED
    assert result.reason == ""


def test_explanation_result_normalizes_claim_kind():
    result = ExplanationResult(provider="null", claim_kind="OBSERVED")
    assert result.claim_kind is ClaimKind.OBSERVED


def test_explanation_result_rejects_empty_provider():
    with pytest.raises(ValidationError):
        ExplanationResult(text="hello", provider="")


def test_relationship_basis_accepts_evidence_basis_members():
    relationship = ResourceRelationship(
        source_id="a",
        target_id="b",
        relationship_type="same_account",
        basis=EvidenceBasis.INFERRED,
    )
    assert relationship.basis is EvidenceBasis.INFERRED


def test_relationship_basis_still_coerces_strings():
    relationship = ResourceRelationship(
        source_id="a",
        target_id="b",
        relationship_type="same_region",
        basis="deterministic",
    )
    assert relationship.basis is EvidenceBasis.DETERMINISTIC


def test_relationship_basis_default_remains_deterministic():
    relationship = ResourceRelationship(
        source_id="a", target_id="b", relationship_type="same_owner_tag"
    )
    assert relationship.basis is EvidenceBasis.DETERMINISTIC