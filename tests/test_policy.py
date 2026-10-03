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
    POLICY_RULE_EC2_STATE_UNRECOGNIZED,
    POLICY_RULE_EC2_STOP_ELIGIBLE,
    POLICY_RULE_EC2_STOP_NOT_ELIGIBLE,
    POLICY_RULE_MISSING_OWNER_TAG,
    POLICY_RULE_OWNER_UNVERIFIABLE,
    Ec2InstanceState,
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
            SWSResourceType.EC2_INSTANCE,
        ],
        resources=list(resources),
        partial=partial,
        truncated=truncated,
    )


def _instance(state, instance_id="i-0abc123def4567890", **overrides) -> ResourceRecord:
    """An EC2 record shaped exactly as ``Ec2InstanceCollector`` emits one.

    ``owner_tag`` is None on purpose: the collector has no tag lookup, so a
    realistic EC2 record never carries one.
    """
    fields = {
        "resource_id": instance_id,
        "resource_type": SWSResourceType.EC2_INSTANCE,
        "name": instance_id,
        "region": "us-east-1",
        "account_id": "123456789012",
        "owner_tag": None,
        "state": state,
    }
    fields.update(overrides)
    return ResourceRecord(**fields)


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


# ---------------------------------------------------------------------------
# EC2 lifecycle state (M13-A)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state", [Ec2InstanceState.RUNNING, Ec2InstanceState.PENDING]
)
def test_stop_eligible_states_recommend_stop_resource(state):
    decision = ENGINE.evaluate(_instance(state.value))
    assert decision.recommended_action is PotentialAction.STOP_RESOURCE
    assert decision.risk_level is RiskLevel.MEDIUM
    assert decision.needs_approval is True
    assert decision.rule == POLICY_RULE_EC2_STOP_ELIGIBLE
    assert decision.confidence == 1.0
    assert decision.evidence == [
        {
            "rule": POLICY_RULE_EC2_STOP_ELIGIBLE,
            "attribute": "state",
            "value": state.value,
        },
        {"attribute": "resource_type", "value": "ec2_instance"},
    ]


@pytest.mark.parametrize(
    "state",
    [
        Ec2InstanceState.STOPPING,
        Ec2InstanceState.STOPPED,
        Ec2InstanceState.SHUTTING_DOWN,
        Ec2InstanceState.TERMINATED,
    ],
)
def test_ineligible_states_recommend_nothing_and_say_why(state):
    decision = ENGINE.evaluate(_instance(state.value))
    assert decision.recommended_action is PotentialAction.LEAVE
    assert decision.rule == POLICY_RULE_EC2_STOP_NOT_ELIGIBLE
    assert decision.needs_approval is False
    assert decision.confidence == 1.0
    assert decision.evidence == [
        {
            "rule": POLICY_RULE_EC2_STOP_NOT_ELIGIBLE,
            "attribute": "state",
            "value": state.value,
        }
    ]
    assert state.value in decision.rationale


@pytest.mark.parametrize(
    "state",
    [
        None,
        "",
        "   ",
        "RUNNING",
        "Running",
        "running ",
        " running",
        "unknown",
        "stopped-but-really",
    ],
)
def test_unrecognized_state_is_flagged_for_review_and_never_stopped(state):
    """No state may be coerced, defaulted, or aliased into a decision.

    An unrecognized value must produce neither ``STOP_RESOURCE`` nor a
    confident ``LEAVE``: the first would act on a state SWS does not
    understand, and the second would assert safety it never established.
    """
    decision = ENGINE.evaluate(_instance(state))
    assert decision.recommended_action is PotentialAction.FLAG_FOR_REVIEW
    assert decision.rule == POLICY_RULE_EC2_STATE_UNRECOGNIZED
    assert decision.evidence == [
        {
            "rule": POLICY_RULE_EC2_STATE_UNRECOGNIZED,
            "attribute": "state",
            "value": state,
        }
    ]


def test_an_already_stopped_instance_is_never_recommended_for_stopping():
    """``STOP_RESOURCE`` is a *reversible* stop, so a stopped instance is exempt."""
    stopped = ENGINE.evaluate(_instance(Ec2InstanceState.STOPPED.value))
    assert stopped.recommended_action is not PotentialAction.STOP_RESOURCE
    assert "already stopped" in stopped.rationale


def test_a_terminated_instance_is_never_recommended_for_stopping():
    """A terminated instance is past the point where a stop is possible."""
    decision = ENGINE.evaluate(_instance(Ec2InstanceState.TERMINATED.value))
    assert decision.recommended_action is not PotentialAction.STOP_RESOURCE
    assert decision.rule == POLICY_RULE_EC2_STOP_NOT_ELIGIBLE


def test_partial_snapshot_does_not_suppress_a_stop_from_an_observed_state():
    """Completeness undermines conclusions from an absence, not from a value.

    ``running`` is what the API returned for this instance. Refusing to
    recommend the stop because some *other* resource type failed to collect
    would make the action unreachable on any real workspace without making the
    recommendation any safer.
    """
    for flags in ({"partial": True}, {"truncated": True}, {"partial": True, "truncated": True}):
        decision = ENGINE.evaluate(_instance("running"), **flags)
        assert decision.recommended_action is PotentialAction.STOP_RESOURCE, flags
        assert decision.rule == POLICY_RULE_EC2_STOP_ELIGIBLE, flags


def test_ec2_records_never_produce_an_owner_tag_finding():
    """A never-collected Owner tag is not evidence of unowned ownership.

    The EC2 collector has no tag lookup, so every instance arrives with
    ``owner_tag=None``. Running the missing-owner-tag rules over those records
    would report unknown ownership for the entire account while establishing
    nothing about any instance.
    """
    for flags in ({}, {"partial": True}, {"truncated": True}):
        for state in ("running", "stopped", None):
            decision = ENGINE.evaluate(_instance(state), **flags)
            assert decision.rule not in (
                POLICY_RULE_MISSING_OWNER_TAG,
                POLICY_RULE_OWNER_UNVERIFIABLE,
            ), (state, flags)


def test_an_ec2_owner_tag_never_becomes_a_decision_input():
    """Even a present Owner tag must not change an EC2 state decision."""
    tagged = _instance("running", owner_tag="alice")
    assert ENGINE.evaluate(tagged) == ENGINE.evaluate(_instance("running"))


def test_ec2_rules_are_keyed_on_resource_type_not_on_the_presence_of_a_state():
    """A Lambda function reporting ``running`` is not an EC2 instance.

    ``ResourceRecord.state`` is generic, so a non-EC2 record carrying an
    EC2-looking state must still fall through to the normal rules rather than
    being stopped on the strength of a vocabulary that does not apply to it.
    """
    decision = ENGINE.evaluate(
        ResourceRecord(
            resource_id="fn",
            resource_type=SWSResourceType.LAMBDA_FUNCTION,
            state="running",
            owner_tag="alice",
        )
    )
    assert decision.recommended_action is PotentialAction.LEAVE
    assert decision.rule is None


def test_ec2_decisions_are_deterministic():
    for state in ("running", "stopped", None, "weird"):
        first = ENGINE.evaluate(_instance(state))
        second = ENGINE.evaluate(_instance(state))
        assert first == second
        assert first.model_dump() == second.model_dump()


def test_ec2_evaluation_does_not_mutate_the_record():
    resource = _instance("running")
    before = resource.model_dump()
    ENGINE.evaluate(resource, partial=True, truncated=True)
    assert resource.model_dump() == before


def test_evaluate_workspace_reports_a_stop_for_a_running_instance():
    snapshot = _snapshot(_instance("running"), _instance("stopped", "i-stopped"))
    decisions = {d.resource_id: d for d in evaluate_workspace(snapshot)}
    assert decisions["i-0abc123def4567890"].recommended_action is (
        PotentialAction.STOP_RESOURCE
    )
    assert decisions["i-0abc123def4567890"].needs_approval is True
    assert decisions["i-stopped"].recommended_action is PotentialAction.LEAVE


def test_evaluate_workspace_decision_ids_stay_distinct_per_rule():
    """A stop and a leave on one snapshot must not collide on decision id."""
    snapshot = _snapshot(_instance("running", "i-run"), _instance("stopped", "i-stop"))
    decisions = {d.resource_id: d for d in evaluate_workspace(snapshot)}
    ids = {d.decision_id for d in decisions.values()}
    assert len(ids) == 2
    assert all(d.snapshot_id == "snap-1" for d in decisions.values())


def test_evaluate_workspace_is_repeatable_with_ec2_records():
    snapshot = _snapshot(_instance("running"), _instance(None, "i-nostate"))
    assert evaluate_workspace(snapshot) == evaluate_workspace(snapshot)


def test_ec2_rule_fires_before_the_owner_tag_rules():
    """The state decision wins, so a running instance is never merely flagged.

    If the owner-tag rules ran first, every EC2 instance would end up
    ``FLAG_FOR_REVIEW`` and ``STOP_RESOURCE`` would be unreachable in
    production -- the exact failure M13-A exists to remove.
    """
    decision = ENGINE.evaluate(_instance("running", owner_tag=None))
    assert decision.recommended_action is PotentialAction.STOP_RESOURCE
    assert decision.rule == POLICY_RULE_EC2_STOP_ELIGIBLE