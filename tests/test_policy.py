"""Deterministic workspace policy layer (M2C-D) with hermetic fixtures.

No network, no AWS, no credentials, no boto3, no LLM. Covers the
WorkspacePolicyEngine implementing the existing PolicyEngine protocol and
the snapshot-aware evaluate_workspace helper.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sws_agent.constants import (
    POLICY_RULE_MISSING_OWNER_TAG,
    POLICY_RULE_OWNER_UNVERIFIABLE,
    PotentialAction,
    RiskLevel,
    SWSResourceType,
)
from sws_agent.interfaces import PolicyEngine
from sws_agent.models import (
    PolicyDecision,
    ResourceRecord,
    ResourceRelationship,
    WorkspaceSnapshot,
)
from sws_agent.policy import WorkspacePolicyEngine, evaluate_workspace

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

BUCKET_ARN = "arn:aws:s3:::bucket-a"
LAMBDA_ARN = "arn:aws:lambda:us-east-1:123456789012:function:fn"


def _resource(resource_id: str, **overrides) -> ResourceRecord:
    fields = {
        "resource_id": resource_id,
        "resource_type": SWSResourceType.S3_BUCKET,
        "account_id": "123456789012",
        "region": "us-east-1",
    }
    fields.update(overrides)
    return ResourceRecord(**fields)


def _snapshot(*resources, partial=False, truncated=False) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        snapshot_id="snap-1",
        created_at=NOW,
        regions=["us-east-1"],
        resource_types=[
            SWSResourceType.S3_BUCKET,
            SWSResourceType.LAMBDA_FUNCTION,
        ],
        resources=list(resources),
        partial=partial,
        truncated=truncated,
    )


ENGINE = WorkspacePolicyEngine()


def test_engine_implements_policy_engine_protocol():
    assert isinstance(ENGINE, PolicyEngine)


def test_default_decision_is_leave_without_rule():
    decision = ENGINE.evaluate(_resource("bucket-a", owner_tag="alice"))
    assert decision.recommended_action is PotentialAction.LEAVE
    assert decision.risk_level is RiskLevel.NONE
    assert decision.confidence == 1.0
    assert decision.needs_approval is False
    assert decision.rule is None
    assert decision.evidence == []


def test_missing_owner_tag_rule_fires():
    decision = ENGINE.evaluate(_resource("bucket-a", owner_tag=None))
    assert decision.recommended_action is PotentialAction.FLAG_FOR_REVIEW
    assert decision.risk_level is RiskLevel.LOW
    assert decision.rule == POLICY_RULE_MISSING_OWNER_TAG
    assert decision.evidence == [
        {
            "rule": POLICY_RULE_MISSING_OWNER_TAG,
            "attribute": "owner_tag",
            "value": None,
        }
    ]


def test_exact_evidence_is_deterministic_and_sorted():
    first = ENGINE.evaluate(_resource("bucket-a", owner_tag=None))
    second = ENGINE.evaluate(_resource("bucket-a", owner_tag=None))
    assert first == second
    assert first.model_dump() == second.model_dump()


def test_owner_unverifiable_under_partial():
    decision = ENGINE.evaluate(_resource("bucket-a", owner_tag=None), partial=True)
    assert decision.recommended_action is PotentialAction.FLAG_FOR_REVIEW
    assert decision.rule == POLICY_RULE_OWNER_UNVERIFIABLE
    assert decision.confidence == 0.5
    assert decision.evidence == [
        {
            "rule": POLICY_RULE_OWNER_UNVERIFIABLE,
            "attribute": "owner_tag",
            "value": None,
        },
        {"attribute": "snapshot_partial", "value": True},
        {"attribute": "snapshot_truncated", "value": False},
    ]


def test_owner_unverifiable_under_truncated():
    decision = ENGINE.evaluate(
        _resource("bucket-a", owner_tag=None), truncated=True
    )
    assert decision.rule == POLICY_RULE_OWNER_UNVERIFIABLE
    assert decision.confidence == 0.5
    assert decision.evidence[-2:] == [
        {"attribute": "snapshot_partial", "value": False},
        {"attribute": "snapshot_truncated", "value": True},
    ]


def test_owner_unverifiable_under_partial_and_truncated():
    decision = ENGINE.evaluate(
        _resource("bucket-a", owner_tag=None), partial=True, truncated=True
    )
    assert decision.rule == POLICY_RULE_OWNER_UNVERIFIABLE
    assert decision.evidence[-2:] == [
        {"attribute": "snapshot_partial", "value": True},
        {"attribute": "snapshot_truncated", "value": True},
    ]


def test_present_owner_tag_not_flagged_even_in_partial_snapshot():
    decision = ENGINE.evaluate(
        _resource("bucket-a", owner_tag="alice"), partial=True, truncated=True
    )
    assert decision.recommended_action is PotentialAction.LEAVE
    assert decision.rule is None


def test_absent_facts_do_not_create_false_certainty():
    decision = ENGINE.evaluate(
        ResourceRecord(
            resource_id="sparse",
            resource_type=SWSResourceType.LAMBDA_FUNCTION,
            owner_tag="alice",
        )
    )
    assert decision.recommended_action is PotentialAction.LEAVE
    assert decision.rule is None
    assert decision.evidence == []


def test_input_record_is_not_mutated():
    resource = _resource("bucket-a", owner_tag=None)
    before = resource.model_dump()
    ENGINE.evaluate(resource, partial=True)
    assert resource.model_dump() == before


def test_workspace_evaluation_is_sorted_and_per_resource():
    snapshot = _snapshot(
        _resource("z-bucket", owner_tag=None),
        _resource("a-bucket", owner_tag="alice"),
        _resource(
            LAMBDA_ARN, resource_type=SWSResourceType.LAMBDA_FUNCTION, owner_tag=None
        ),
    )
    decisions = evaluate_workspace(snapshot)
    assert [decision.resource_id for decision in decisions] == sorted(
        decision.resource_id for decision in decisions
    )
    assert len(decisions) == 3
    rules = {decision.resource_id: decision.rule for decision in decisions}
    assert rules["a-bucket"] is None
    assert rules["z-bucket"] == POLICY_RULE_MISSING_OWNER_TAG
    assert rules[LAMBDA_ARN] == POLICY_RULE_MISSING_OWNER_TAG


def test_partial_snapshot_uses_unverifiable_rule():
    snapshot = _snapshot(
        _resource("bucket-a", owner_tag=None),
        partial=True,
    )
    decisions = evaluate_workspace(snapshot)
    assert len(decisions) == 1
    assert decisions[0].rule == POLICY_RULE_OWNER_UNVERIFIABLE
    assert decisions[0].confidence == 0.5


def test_truncated_snapshot_uses_unverifiable_rule():
    snapshot = _snapshot(
        _resource("bucket-a", owner_tag=None),
        truncated=True,
    )
    decisions = evaluate_workspace(snapshot)
    assert len(decisions) == 1
    assert decisions[0].rule == POLICY_RULE_OWNER_UNVERIFIABLE


def test_empty_snapshot_yields_empty_decisions():
    assert evaluate_workspace(_snapshot()) == []


def test_workspace_evaluation_is_repeatable():
    snapshot = _snapshot(
        _resource("bucket-a", owner_tag=None),
        _resource("bucket-b", owner_tag="bob"),
        partial=True,
    )
    assert evaluate_workspace(snapshot) == evaluate_workspace(snapshot)


def test_provided_relationships_do_not_change_outcomes():
    snapshot = _snapshot(
        _resource("bucket-a", owner_tag="alice"),
        _resource("bucket-b", owner_tag="alice"),
    )
    relationships = [
        ResourceRelationship(
            source_id="bucket-a",
            target_id="bucket-b",
            relationship_type="same_owner_tag",
        )
    ]
    with_relationships = evaluate_workspace(snapshot, relationships=relationships)
    without = evaluate_workspace(snapshot)
    assert with_relationships == without
    assert relationships[0].source_id == "bucket-a"  # not mutated


def test_absent_relationships_never_treated_as_false():
    snapshot = _snapshot(
        _resource("bucket-a", owner_tag="alice"),
        _resource("bucket-b", owner_tag="bob"),
        partial=True,
    )
    decisions = evaluate_workspace(snapshot)
    for decision in decisions:
        assert decision.rule is None
        assert not any(
            entry.get("attribute") == "relationship_absent"
            for entry in decision.evidence
        )


def test_engine_has_no_action_execution_surface():
    engine = WorkspacePolicyEngine()
    assert not hasattr(engine, "execute")
    decision = engine.evaluate(_resource("bucket-a", owner_tag=None))
    assert isinstance(decision, PolicyDecision)


def test_policy_decision_model_stays_additive():
    valid = PolicyDecision(
        resource_id="x", recommended_action=PotentialAction.LEAVE
    )
    assert valid.rule is None
    assert valid.evidence == []
    with pytest.raises(ValidationError):
        PolicyDecision(
            resource_id="x",
            recommended_action=PotentialAction.LEAVE,
            rule="",
        )