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
    MAX_COST_GROUP_BY_KEYS,
    MAX_COST_WINDOW_DAYS,
    MAX_RESOURCES_PER_INVENTORY_REQUEST,
    MAX_TRACE_EVENTS,
    SMS_DEPRECATED_ACTIONS,
    SWS_SUPPORTED_ACTIONS,
    SWS_SUPPORTED_COLLECTION_FAILURE_CATEGORIES,
    SWS_SUPPORTED_EXECUTION_MODES,
    SWS_SUPPORTED_RESOURCE_TYPES,
    CollectionFailureCategory,
    ExecutionMode,
    PotentialAction,
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