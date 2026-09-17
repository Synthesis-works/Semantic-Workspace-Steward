"""SWS domain models: enum normalization and field constraints."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sws_agent.constants import (
    EvidenceBasis,
    ExecutionMode,
    PotentialAction,
    RiskLevel,
    SWSResourceType,
)
from sws_agent.models import (
    AuthorizationRequest,
    CostEstimate,
    PolicyDecision,
    ResourceRecord,
    ResourceRelationship,
)


def test_resource_record_normalizes_resource_type_case_insensitively():
    record = ResourceRecord(resource_id="rn-123", resource_type="S3_Bucket")
    assert record.resource_type is SWSResourceType.S3_BUCKET


def test_resource_record_rejects_unknown_resource_type():
    with pytest.raises(ValidationError):
        ResourceRecord(resource_id="rn-123", resource_type="kinesis-stream")


def test_resource_record_rejects_empty_id():
    with pytest.raises(ValidationError):
        ResourceRecord(resource_id="", resource_type=SWSResourceType.S3_BUCKET)


def test_policy_decision_normalizes_action_and_risk():
    decision = PolicyDecision(
        resource_id="rn-1",
        recommended_action="STOP_RESOURCE",
        risk_level="MEDIUM",
    )
    assert decision.recommended_action is PotentialAction.STOP_RESOURCE
    assert decision.risk_level is RiskLevel.MEDIUM


def test_policy_decision_rejects_confidence_out_of_bounds():
    with pytest.raises(ValidationError):
        PolicyDecision(
            resource_id="rn-1",
            recommended_action=PotentialAction.LEAVE,
            confidence=1.5,
        )


def test_authorization_request_normalizes_all_enum_fields():
    request = AuthorizationRequest(
        resource_id="rn-1",
        resource_type="lambda_function",
        action="flag_for_review",
        execution_mode="autonomous",
    )
    assert request.resource_type is SWSResourceType.LAMBDA_FUNCTION
    assert request.action is PotentialAction.FLAG_FOR_REVIEW
    assert request.execution_mode is ExecutionMode.AUTONOMOUS


def test_cost_estimate_rejects_negative_amount():
    with pytest.raises(ValidationError):
        CostEstimate(line_item="bucket storage", amount_usd=-1.0, basis="pricing page")


def test_cost_estimate_defaults_to_projected():
    estimate = CostEstimate(line_item="bucket storage", amount_usd=1.25, basis="pricing page")
    assert estimate.projected is True


def test_relationship_defaults_to_deterministic_evidence():
    relationship = ResourceRelationship(
        source_id="a", target_id="b", relationship_type="uses"
    )
    assert relationship.basis is EvidenceBasis.DETERMINISTIC


def test_relationship_normalizes_relationship_type():
    relationship = ResourceRelationship(
        source_id="a", target_id="b", relationship_type="USES"
    )
    assert relationship.relationship_type == "uses"


def test_relationship_rejects_confidence_out_of_bounds():
    with pytest.raises(ValidationError):
        ResourceRelationship(
            source_id="a",
            target_id="b",
            relationship_type="uses",
            confidence=2.0,
        )