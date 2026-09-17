"""Deterministic authorization gate.

Tests pin the decision matrix: zero-side-effect actions are authorized in
all modes, REQUEST_APPROVAL always implies pending approval, STOP_RESOURCE
requires approval outside AUTONOMOUS mode, and unknown actions are blocked.
All decisions are rule-based (never LLM) and deterministic.
"""

from __future__ import annotations

from sws_agent.authorization import ActionAuthorizer
from sws_agent.constants import (
    AuthorizationDecision,
    ExecutionMode,
    PotentialAction,
    SWSResourceType,
)
from sws_agent.models import AuthorizationRequest

AUTHORIZER = ActionAuthorizer()


def _request(
    action: PotentialAction,
    execution_mode: ExecutionMode = ExecutionMode.SAFE,
) -> AuthorizationRequest:
    return AuthorizationRequest(
        resource_id="bucket-example",
        resource_type=SWSResourceType.S3_BUCKET,
        action=action,
        execution_mode=execution_mode,
    )


def test_zero_side_effect_actions_authorized_in_all_modes():
    for mode in ExecutionMode:
        for action in (PotentialAction.LEAVE, PotentialAction.FLAG_FOR_REVIEW):
            result = AUTHORIZER.authorize(_request(action, mode))
            assert result.decision is AuthorizationDecision.AUTHORIZED
            assert result.requires_human_approval is False


def test_request_approval_always_pending():
    for mode in ExecutionMode:
        result = AUTHORIZER.authorize(_request(PotentialAction.REQUEST_APPROVAL, mode))
        assert result.decision is AuthorizationDecision.PENDING_APPROVAL
        assert result.requires_human_approval is True


def test_stop_requires_approval_in_safe_and_review_modes():
    for mode in (ExecutionMode.SAFE, ExecutionMode.REVIEW):
        result = AUTHORIZER.authorize(_request(PotentialAction.STOP_RESOURCE, mode))
        assert result.decision is AuthorizationDecision.PENDING_APPROVAL
        assert result.requires_human_approval is True


def test_stop_authorized_in_autonomous_mode():
    result = AUTHORIZER.authorize(
        _request(PotentialAction.STOP_RESOURCE, ExecutionMode.AUTONOMOUS)
    )
    assert result.decision is AuthorizationDecision.AUTHORIZED
    assert result.requires_human_approval is False


def test_nonexistent_action_blocked():
    """An action outside the canonical vocabulary must be BLOCKED.

    model_construct skips validation so we can exercise the authorizer's
    defensive branch against an out-of-vocabulary action value.
    """
    request = AuthorizationRequest.model_construct(
        resource_id="bucket-example",
        resource_type=SWSResourceType.S3_BUCKET,
        action=object(),
        execution_mode=ExecutionMode.SAFE,
    )
    result = AUTHORIZER.authorize(request)
    assert result.decision is AuthorizationDecision.BLOCKED


def test_decision_is_deterministic_for_same_input():
    request = _request(PotentialAction.STOP_RESOURCE, ExecutionMode.SAFE)
    first = AUTHORIZER.authorize(request)
    second = AUTHORIZER.authorize(request)
    assert first == second


def test_string_inputs_are_normalized_case_insensitively():
    request = AuthorizationRequest(
        resource_id="bucket-example",
        resource_type="S3_BUCKET",
        action="STOP_RESOURCE",
        execution_mode="safe",
    )
    result = AUTHORIZER.authorize(request)
    assert result.decision is AuthorizationDecision.PENDING_APPROVAL