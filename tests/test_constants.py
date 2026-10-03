"""Canonical vocabulary invariants.

These tests pin the single-source-of-truth property of
``sws_agent.constants``: the action vocabulary, resource types, and
execution modes are defined once and never drift into renamed SMS
vocabulary.
"""

from __future__ import annotations

from sws_agent.constants import (
    AWS_API_RETRY_ATTEMPTS,
    AWS_API_TIMEOUT_SECONDS,
    COST_GROUP_DIMENSION_SERVICE,
    COST_GROUP_TAG_OWNER,
    EC2_STOP_ELIGIBLE_STATES,
    EC2_STOP_INELIGIBLE_STATES,
    MAX_COST_GROUP_BY_KEYS,
    MAX_COST_WINDOW_DAYS,
    MAX_RESOURCES_FOR_RELATIONSHIP_DERIVATION,
    MAX_RESOURCES_PER_INVENTORY_REQUEST,
    MAX_TRACE_EVENTS,
    OWNER_TAG_COLLECTED_RESOURCE_TYPES,
    POLICY_RULE_EC2_STATE_UNRECOGNIZED,
    POLICY_RULE_EC2_STOP_ELIGIBLE,
    POLICY_RULE_EC2_STOP_NOT_ELIGIBLE,
    POLICY_RULE_MISSING_OWNER_TAG,
    POLICY_RULE_OWNER_UNVERIFIABLE,
    SMS_DEPRECATED_ACTIONS,
    SWS_SUPPORTED_ACTIONS,
    SWS_SUPPORTED_COLLECTION_FAILURE_CATEGORIES,
    SWS_SUPPORTED_COST_GROUP_BY_KEYS,
    SWS_SUPPORTED_EC2_INSTANCE_STATES,
    SWS_SUPPORTED_EXECUTION_MODES,
    SWS_SUPPORTED_POLICY_RULES,
    SWS_SUPPORTED_RELATIONSHIP_TYPES,
    SWS_SUPPORTED_RESOURCE_TYPES,
    CollectionFailureCategory,
    Ec2InstanceState,
    ExecutionMode,
    PotentialAction,
    RelationshipType,
    SWSResourceType,
)


def test_action_vocabulary_has_no_duplicate_values():
    values = [action.value for action in PotentialAction]
    assert len(values) == len(set(values)), "action enum contains duplicate values"


def test_resource_types_have_no_duplicate_values():
    values = [resource.value for resource in SWSResourceType]
    assert len(values) == len(set(values)), "resource enum contains duplicate values"


def test_execution_modes_have_no_duplicate_values():
    values = [mode.value for mode in ExecutionMode]
    assert len(values) == len(set(values)), "execution mode enum contains duplicate values"


def test_collection_failure_categories_have_no_duplicate_values():
    values = [category.value for category in CollectionFailureCategory]
    assert len(values) == len(set(values)), (
        "collection failure category enum contains duplicate values"
    )
    assert SWS_SUPPORTED_COLLECTION_FAILURE_CATEGORIES == {
        category.value for category in CollectionFailureCategory
    }


def test_relationship_types_have_no_duplicate_values():
    values = [relationship.value for relationship in RelationshipType]
    assert len(values) == len(set(values)), (
        "relationship type enum contains duplicate values"
    )
    assert SWS_SUPPORTED_RELATIONSHIP_TYPES == {
        relationship.value for relationship in RelationshipType
    }


def test_relationship_derivation_safety_cap_is_positive():
    assert MAX_RESOURCES_FOR_RELATIONSHIP_DERIVATION >= 1


def test_policy_rule_ids_have_no_duplicate_values():
    rule_ids = [
        POLICY_RULE_MISSING_OWNER_TAG,
        POLICY_RULE_OWNER_UNVERIFIABLE,
        POLICY_RULE_EC2_STOP_ELIGIBLE,
        POLICY_RULE_EC2_STOP_NOT_ELIGIBLE,
        POLICY_RULE_EC2_STATE_UNRECOGNIZED,
    ]
    assert len(rule_ids) == len(set(rule_ids)), (
        "policy rule identifiers contain duplicates"
    )
    assert SWS_SUPPORTED_POLICY_RULES == set(rule_ids)


def test_ec2_instance_states_have_no_duplicate_values():
    values = [state.value for state in Ec2InstanceState]
    assert len(values) == len(set(values)), (
        "EC2 instance state enum contains duplicate values"
    )
    assert SWS_SUPPORTED_EC2_INSTANCE_STATES == set(values)


def test_every_ec2_state_is_classified_as_eligible_or_ineligible():
    """No state may fall between the two buckets.

    A state in neither set would reach policy as unrecognized, so an omitted
    state would silently become a review flag rather than a decision. The two
    sets must therefore partition the documented vocabulary exactly.
    """
    eligible = {state.value for state in EC2_STOP_ELIGIBLE_STATES}
    ineligible = {state.value for state in EC2_STOP_INELIGIBLE_STATES}
    assert eligible.isdisjoint(ineligible), (
        "a state cannot be both stop-eligible and stop-ineligible"
    )
    assert eligible | ineligible == set(SWS_SUPPORTED_EC2_INSTANCE_STATES), (
        "every documented EC2 state must be explicitly classified"
    )


def test_ec2_stop_eligible_states_are_reversible_by_definition():
    """``terminated`` and ``shutting-down`` can never be stopped."""
    assert Ec2InstanceState.TERMINATED not in EC2_STOP_ELIGIBLE_STATES
    assert Ec2InstanceState.SHUTTING_DOWN not in EC2_STOP_ELIGIBLE_STATES
    assert Ec2InstanceState.STOPPED in EC2_STOP_INELIGIBLE_STATES


def test_owner_tag_rules_are_scoped_to_types_that_collect_the_tag():
    """The EC2 collector never reads tags, so EC2 must not be in this set.

    ``policy.py`` consults this set to decide whether an absent Owner tag is a
    finding about a resource or merely an attribute SWS never collected. If
    EC2 were listed, every instance in the account would be reported as having
    unknown ownership on the strength of a lookup that never happened.
    """
    assert SWSResourceType.EC2_INSTANCE not in OWNER_TAG_COLLECTED_RESOURCE_TYPES
    assert SWSResourceType.S3_BUCKET in OWNER_TAG_COLLECTED_RESOURCE_TYPES
    assert SWSResourceType.LAMBDA_FUNCTION in OWNER_TAG_COLLECTED_RESOURCE_TYPES


def test_action_vocabulary_never_drifts_into_sms_lifecycle_states():
    """SWS actions must not become renamed copies of SMS file-lifecycle states."""
    assert SWS_SUPPORTED_ACTIONS.isdisjoint(SMS_DEPRECATED_ACTIONS)


def test_support_sets_match_enum_definitions():
    assert SWS_SUPPORTED_ACTIONS == {action.value for action in PotentialAction}
    assert SWS_SUPPORTED_RESOURCE_TYPES == {
        resource.value for resource in SWSResourceType
    }
    assert SWS_SUPPORTED_EXECUTION_MODES == {mode.value for mode in ExecutionMode}


def test_action_vocabulary_contains_documented_concepts():
    assert "leave" in SWS_SUPPORTED_ACTIONS
    assert "flag_for_review" in SWS_SUPPORTED_ACTIONS
    assert "request_approval" in SWS_SUPPORTED_ACTIONS
    assert "stop_resource" in SWS_SUPPORTED_ACTIONS


def test_limits_are_positive_and_documented_purposes_hold():
    assert MAX_RESOURCES_PER_INVENTORY_REQUEST >= 1
    assert MAX_COST_WINDOW_DAYS >= 1
    assert MAX_COST_GROUP_BY_KEYS >= 1
    assert AWS_API_RETRY_ATTEMPTS >= 1
    assert AWS_API_TIMEOUT_SECONDS >= 1
    assert MAX_TRACE_EVENTS >= 1


def test_cost_window_never_exceeds_cost_explorer_daily_ceiling():
    """Cost Explorer's daily-granularity history limit is 366 days; SWS caps lower."""
    assert MAX_COST_WINDOW_DAYS <= 366


def test_cost_group_by_keys_have_no_duplicate_values():
    keys = [COST_GROUP_DIMENSION_SERVICE, COST_GROUP_TAG_OWNER]
    assert len(keys) == len(set(keys)), "cost group-by keys contain duplicates"
    assert SWS_SUPPORTED_COST_GROUP_BY_KEYS == set(keys)
    assert len(SWS_SUPPORTED_COST_GROUP_BY_KEYS) <= MAX_COST_GROUP_BY_KEYS


def test_cost_group_by_support_set_matches_documented_vocabulary():
    assert COST_GROUP_DIMENSION_SERVICE in SWS_SUPPORTED_COST_GROUP_BY_KEYS
    assert COST_GROUP_TAG_OWNER in SWS_SUPPORTED_COST_GROUP_BY_KEYS