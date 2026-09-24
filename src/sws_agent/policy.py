"""Deterministic workspace policy layer (M2C-D).

Pure decision/reporting layer over the canonical resource model
(``ResourceRecord``), the M2C-B snapshot completeness contract, and the
M2C-C deterministic relationships. Implements the existing ``PolicyEngine``
protocol from ``interfaces.py`` and is deterministic, LLM-free,
credential-free, and independently testable.

Philosophy: *AI understands. Deterministic policy decides.* The policy
engine only produces ``PolicyDecision`` reports. It never executes an
action, never authorizes, never calls AWS, and never invokes an LLM.

Rules are deliberately small and grounded only in facts the canonical
model already represents. Every rule has a stable identifier
(``constants.POLICY_RULE_*``), a deterministic predicate, and structured
evidence attached to the decision.

Partial / truncated contract (M2C-B semantics): an absent Owner tag may be
a genuine fact or a collection artifact when the workspace inventory is
partial or truncated. The engine never manufactures certainty from such an
absence: under a non-authoritative snapshot the unverifiable case is
reported instead of a confident missing-tag conclusion. A relationship's
absence likewise never implies the relationship is false.

Relationships (M2C-C): no current rule consumes relationships, because the
only controlled types (``same_account`` / ``same_region`` /
``same_owner_tag``) carry no isolated, non-inferring policy trigger, and
``same_owner_tag`` must never be reinterpreted as proof of ownership. The
workspace evaluator accepts relationships as context for forward
compatibility without letting them change outcomes.
"""

from __future__ import annotations

from typing import Any, Iterable

from .constants import (
    POLICY_RULE_MISSING_OWNER_TAG,
    POLICY_RULE_OWNER_UNVERIFIABLE,
    PotentialAction,
    RiskLevel,
)
from .models import (
    PolicyDecision,
    ResourceRecord,
    ResourceRelationship,
    WorkspaceSnapshot,
)


class WorkspacePolicyEngine:
    """Deterministic policy engine implementing ``interfaces.PolicyEngine``.

    Conforms to the existing protocol for the ``evaluate(resource)`` call
    and additionally accepts optional completeness flags so M2C-B snapshot
    semantics can be honored. The engine is stateless: identical inputs
    always produce identical outputs, and inputs are never mutated.
    """

    def evaluate(
        self,
        resource: ResourceRecord,
        *,
        partial: bool = False,
        truncated: bool = False,
    ) -> PolicyDecision:
        """Evaluate one canonical resource into a ``PolicyDecision`` report.

        ``partial`` / ``truncated`` describe the workspace that owned the
        record (M2C-B semantics). When either is True, an absent Owner tag
        is reported as unverifiable rather than as a confident missing-tag
        conclusion. This returns a report only; it never executes an action.
        """
        partial = bool(partial)
        truncated = bool(truncated)

        if resource.owner_tag is None:
            if partial or truncated:
                return self._owner_unverifiable(
                    resource, partial=partial, truncated=truncated
                )
            return self._missing_owner_tag(resource)
        return PolicyDecision(
            resource_id=resource.resource_id,
            recommended_action=PotentialAction.LEAVE,
            rationale=(
                f"no deterministic policy rule triggered for "
                f"'{resource.resource_id}'"
            ),
        )

    @staticmethod
    def _missing_owner_tag(resource: ResourceRecord) -> PolicyDecision:
        return PolicyDecision(
            resource_id=resource.resource_id,
            recommended_action=PotentialAction.FLAG_FOR_REVIEW,
            risk_level=RiskLevel.LOW,
            rationale=(
                f"resource '{resource.resource_id}' has no recorded Owner "
                "tag; flagging for review so ownership attribution can be "
                "added."
            ),
            confidence=1.0,
            rule=POLICY_RULE_MISSING_OWNER_TAG,
            evidence=[
                {
                    "rule": POLICY_RULE_MISSING_OWNER_TAG,
                    "attribute": "owner_tag",
                    "value": None,
                }
            ],
        )

    @staticmethod
    def _owner_unverifiable(
        resource: ResourceRecord, *, partial: bool, truncated: bool
    ) -> PolicyDecision:
        completeness = []
        if partial:
            completeness.append("partial")
        if truncated:
            completeness.append("truncated")
        return PolicyDecision(
            resource_id=resource.resource_id,
            recommended_action=PotentialAction.FLAG_FOR_REVIEW,
            risk_level=RiskLevel.LOW,
            rationale=(
                f"resource '{resource.resource_id}' has no recorded Owner "
                f"tag, but the workspace inventory is "
                f"{' and '.join(completeness)}; the absence may be a "
                "collection artifact, so ownership cannot be verified "
                "deterministically. Flagging for review instead of "
                "concluding the tag is missing."
            ),
            confidence=0.5,
            rule=POLICY_RULE_OWNER_UNVERIFIABLE,
            evidence=[
                {
                    "rule": POLICY_RULE_OWNER_UNVERIFIABLE,
                    "attribute": "owner_tag",
                    "value": None,
                },
                {"attribute": "snapshot_partial", "value": partial},
                {"attribute": "snapshot_truncated", "value": truncated},
            ],
        )


def evaluate_workspace(
    snapshot: WorkspaceSnapshot,
    *,
    relationships: Iterable[ResourceRelationship] | None = None,
) -> list[PolicyDecision]:
    """Evaluate every resource in a snapshot into sorted policy decisions.

    Completeness flags come from the snapshot itself (M2C-B), so absent
    facts are never presented as certain when the inventory is partial or
    truncated. ``relationships`` is accepted as context for forward
    compatibility; no current rule consumes it, so providing it never
    changes outcomes, and a relationship's absence is never treated as the
    relationship being false.

    The input snapshot (and any provided relationships) are never mutated.
    Output is deterministic and sorted by ``resource_id``.
    """
    engine = WorkspacePolicyEngine()
    decisions = [
        engine.evaluate(
            resource,
            partial=snapshot.partial,
            truncated=snapshot.truncated,
        )
        for resource in snapshot.resources
    ]
    return sorted(decisions, key=lambda decision: decision.resource_id)