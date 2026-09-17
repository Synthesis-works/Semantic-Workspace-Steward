"""Deterministic authorization boundary for SWS actions.

REUSE DECISION (see docs/reuse-decisions.md):

- Reused (pattern only): SMS's authorization.py concept of a rule-based
  safety gate that returns AUTHORIZED / PENDING_APPROVAL / BLOCKED and
  never lets an LLM decide authorization. The decision logic is
  deterministic and independently testable.
- Rewritten: all domain rules, for SWS's AWS action vocabulary and
  execution modes.

Core philosophy: **AI understands. Deterministic policy decides.** The
LLM must never independently authorize risky AWS operations.
"""

from __future__ import annotations

from .constants import (
    AuthorizationDecision,
    ExecutionMode,
    PotentialAction,
    RiskLevel,
)
from .models import AuthorizationRequest, AuthorizationResult

_ACTION_RISK: dict[PotentialAction, RiskLevel] = {
    PotentialAction.LEAVE: RiskLevel.NONE,
    PotentialAction.FLAG_FOR_REVIEW: RiskLevel.NONE,
    PotentialAction.REQUEST_APPROVAL: RiskLevel.LOW,
    PotentialAction.STOP_RESOURCE: RiskLevel.MEDIUM,
}

_ZERO_SIDE_EFFECT_ACTIONS: frozenset[PotentialAction] = frozenset(
    {PotentialAction.LEAVE, PotentialAction.FLAG_FOR_REVIEW}
)
"""Actions that change nothing on AWS and therefore need no approval."""


class ActionAuthorizer:
    """Deterministic, independently testable authorization gate.

    The authorizer is stateless: it renders a decision from the request
    alone. Human-approval tracking lives in the ApprovalProvider, not
    here. No destructive actions exist in the current vocabulary; any
    such action would require a documented safety review.
    """

    def authorize(self, request: AuthorizationRequest) -> AuthorizationResult:
        risk = _ACTION_RISK.get(request.action)
        if risk is None:
            return AuthorizationResult(
                decision=AuthorizationDecision.BLOCKED,
                reason=(
                    f"action '{request.action}' is not part of the supported "
                    "action vocabulary"
                ),
            )

        if request.action in _ZERO_SIDE_EFFECT_ACTIONS:
            return AuthorizationResult(
                decision=AuthorizationDecision.AUTHORIZED,
                reason=(
                    f"{request.action.value} has no side effects and requires "
                    "no approval"
                ),
            )

        if request.action is PotentialAction.REQUEST_APPROVAL:
            return AuthorizationResult(
                decision=AuthorizationDecision.PENDING_APPROVAL,
                reason="the action itself requests a human review",
                requires_human_approval=True,
            )

        # STOP_RESOURCE is the only side-effecting action currently defined.
        if request.execution_mode is ExecutionMode.AUTONOMOUS:
            return AuthorizationResult(
                decision=AuthorizationDecision.AUTHORIZED,
                reason=(
                    "STOP_RESOURCE in AUTONOMOUS mode is reversible and "
                    "approved; verification of the resulting AWS state is "
                    "still mandatory after execution"
                ),
            )

        return AuthorizationResult(
            decision=AuthorizationDecision.PENDING_APPROVAL,
            reason=(
                f"STOP_RESOURCE requires human approval in "
                f"{request.execution_mode.value} mode"
            ),
            requires_human_approval=True,
        )