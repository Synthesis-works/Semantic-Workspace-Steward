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

EC2 lifecycle state (M13-A): a resource's recorded ``state`` is the only
input the engine treats as authority to recommend a side-effecting action. A
``running`` or ``pending`` instance yields ``STOP_RESOURCE``; ``stopping``,
``stopped``, ``shutting-down``, and ``terminated`` yield ``LEAVE``; and a
blank, absent, or unrecognized state is flagged for review rather than mapped
onto a neighbouring state. Two boundary decisions are load-bearing:

* **A positive observed fact survives a partial snapshot.** The existing M2C-B
  contract downgrades conclusions drawn from an *absence* when a snapshot is
  partial or truncated. ``running`` is not an absence: it is a value the API
  returned for this instance. Refusing to recommend a stop merely because some
  *other* resource type failed to collect would make M13 unreachable on any
  real workspace without making the recommendation any safer.
* **An Owner-tag absence is never a finding for a type that never looks.** The
  M13-A EC2 collector is bound to the single read-only
  ``DescribeInstances`` seam, which returns no tags, so every EC2 instance
  arrives with ``owner_tag=None`` regardless of how it is tagged. Running the
  missing-owner-tag rule over those records would report unknown ownership for
  every instance in the account while establishing nothing at all. The
  owner-tag rules are therefore scoped to
  ``constants.OWNER_TAG_COLLECTED_RESOURCE_TYPES``; for a type outside it the
  engine says only what it actually observed, and the absence of an Owner tag
  is reported as an uncollected attribute rather than as a finding.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from .constants import (
    EC2_STOP_ELIGIBLE_STATES,
    EC2_STOP_INELIGIBLE_STATES,
    OWNER_TAG_COLLECTED_RESOURCE_TYPES,
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
from .models import (
    PolicyDecision,
    ResourceRecord,
    ResourceRelationship,
    WorkspaceSnapshot,
)


def _decision_id(
    resource_id: str, *, snapshot_id: str | None, rule: str | None
) -> str:
    """Deterministic decision identity over the decision's derivation inputs.

    The id is a stable hex digest of ``(snapshot_id, resource_id, rule)``:
    re-evaluating the same resource from the same snapshot produces the
    same decision id (so audit replay is repeatable), while the same
    resource evaluated under a different snapshot or rule produces a
    different id (so lineage is distinguishable). Engine-level single
    resource evaluations (no snapshot context) pass ``snapshot_id=None``.
    """
    material = "\x1f".join(
        [snapshot_id or "", resource_id, rule or ""]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _ec2_state(resource: ResourceRecord) -> Ec2InstanceState | None:
    """Resolve a recorded state string to a documented EC2 state, or None.

    Resolution is an exact, case-sensitive match against the values EC2
    documents. No aliasing, case folding, prefix matching, or defaulting: a
    string AWS sent that is not in the vocabulary resolves to ``None``, which
    the caller reports for review. Returning ``None`` must never be read as
    "nothing to do" -- that reading is exactly the one that would let an
    unrecognized state slip past a stop decision unexamined.
    """
    recorded = resource.state
    if not isinstance(recorded, str):
        return None
    try:
        return Ec2InstanceState(recorded)
    except ValueError:
        return None


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

        if resource.resource_type is SWSResourceType.EC2_INSTANCE:
            return self._evaluate_ec2(resource)

        if resource.owner_tag is None:
            if resource.resource_type not in OWNER_TAG_COLLECTED_RESOURCE_TYPES:
                return self._no_rule_fired(resource)
            if partial or truncated:
                return self._owner_unverifiable(
                    resource, partial=partial, truncated=truncated
                )
            return self._missing_owner_tag(resource)
        return self._no_rule_fired(resource)

    def _evaluate_ec2(self, resource: ResourceRecord) -> PolicyDecision:
        """Decide an EC2 instance from its observed lifecycle state alone.

        The recorded ``state`` is matched exactly against the documented EC2
        vocabulary. A value outside it -- including ``None``, an empty string,
        and a future state AWS might add -- is never coerced to the nearest
        member and never treated as benign; it is surfaced for review.
        """
        state = _ec2_state(resource)
        if state in EC2_STOP_ELIGIBLE_STATES:
            return self._ec2_stop_eligible(resource, state)
        if state in EC2_STOP_INELIGIBLE_STATES:
            return self._ec2_stop_not_eligible(resource, state)
        return self._ec2_state_unrecognized(resource)

    @staticmethod
    def _ec2_stop_eligible(
        resource: ResourceRecord, state: Ec2InstanceState
    ) -> PolicyDecision:
        return PolicyDecision(
            resource_id=resource.resource_id,
            recommended_action=PotentialAction.STOP_RESOURCE,
            risk_level=RiskLevel.MEDIUM,
            rationale=(
                f"EC2 instance '{resource.resource_id}' was observed in state "
                f"'{state.value}', from which a stop is valid and reversible; "
                "recommending STOP_RESOURCE for human approval. This is a "
                "recommendation only: no mutation is executed by policy."
            ),
            confidence=1.0,
            needs_approval=True,
            rule=POLICY_RULE_EC2_STOP_ELIGIBLE,
            decision_id=_decision_id(
                resource.resource_id,
                snapshot_id=None,
                rule=POLICY_RULE_EC2_STOP_ELIGIBLE,
            ),
            evidence=[
                {
                    "rule": POLICY_RULE_EC2_STOP_ELIGIBLE,
                    "attribute": "state",
                    "value": state.value,
                },
                {"attribute": "resource_type", "value": "ec2_instance"},
            ],
        )

    @staticmethod
    def _ec2_stop_not_eligible(
        resource: ResourceRecord, state: Ec2InstanceState
    ) -> PolicyDecision:
        return PolicyDecision(
            resource_id=resource.resource_id,
            recommended_action=PotentialAction.LEAVE,
            rationale=(
                f"EC2 instance '{resource.resource_id}' was observed in state "
                f"'{state.value}', in which no stop may be recommended; the "
                "instance is already stopped, already stopping, or past the "
                "point where it can be stopped. SWS recommends nothing and "
                "asserts nothing beyond that observation."
            ),
            confidence=1.0,
            rule=POLICY_RULE_EC2_STOP_NOT_ELIGIBLE,
            decision_id=_decision_id(
                resource.resource_id,
                snapshot_id=None,
                rule=POLICY_RULE_EC2_STOP_NOT_ELIGIBLE,
            ),
            evidence=[
                {
                    "rule": POLICY_RULE_EC2_STOP_NOT_ELIGIBLE,
                    "attribute": "state",
                    "value": state.value,
                }
            ],
        )

    @staticmethod
    def _ec2_state_unrecognized(resource: ResourceRecord) -> PolicyDecision:
        observed = resource.state
        return PolicyDecision(
            resource_id=resource.resource_id,
            recommended_action=PotentialAction.FLAG_FOR_REVIEW,
            risk_level=RiskLevel.LOW,
            rationale=(
                f"EC2 instance '{resource.resource_id}' has no usable "
                "lifecycle state ("
                + (
                    f"recorded as {observed!r}"
                    if isinstance(observed, str) and observed.strip()
                    else "none was recorded"
                )
                + "), and that value is not one of the documented EC2 "
                "instance states. Flagging for review rather than mapping it "
                "onto a neighbouring state: a stop recommendation derived "
                "from a state SWS does not recognize would not be a fact."
            ),
            confidence=1.0,
            rule=POLICY_RULE_EC2_STATE_UNRECOGNIZED,
            decision_id=_decision_id(
                resource.resource_id,
                snapshot_id=None,
                rule=POLICY_RULE_EC2_STATE_UNRECOGNIZED,
            ),
            evidence=[
                {
                    "rule": POLICY_RULE_EC2_STATE_UNRECOGNIZED,
                    "attribute": "state",
                    "value": observed,
                }
            ],
        )

    @staticmethod
    def _no_rule_fired(resource: ResourceRecord) -> PolicyDecision:
        return PolicyDecision(
            resource_id=resource.resource_id,
            recommended_action=PotentialAction.LEAVE,
            rationale=(
                f"no deterministic policy rule triggered for "
                f"'{resource.resource_id}'"
            ),
            decision_id=_decision_id(
                resource.resource_id, snapshot_id=None, rule=None
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
            decision_id=_decision_id(
                resource.resource_id,
                snapshot_id=None,
                rule=POLICY_RULE_MISSING_OWNER_TAG,
            ),
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
            decision_id=_decision_id(
                resource.resource_id,
                snapshot_id=None,
                rule=POLICY_RULE_OWNER_UNVERIFIABLE,
            ),
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
    stamped = [
        (
            decision.model_copy(
                update={
                    "decision_id": _decision_id(
                        decision.resource_id,
                        snapshot_id=snapshot.snapshot_id,
                        rule=decision.rule,
                    ),
                    "snapshot_id": snapshot.snapshot_id,
                    "run_id": snapshot.run_id,
                }
            )
            if decision.snapshot_id is None
            else decision
        )
        for decision in (
            engine.evaluate(
                resource,
                partial=snapshot.partial,
                truncated=snapshot.truncated,
            )
            for resource in snapshot.resources
        )
    ]
    return sorted(stamped, key=lambda decision: decision.resource_id)