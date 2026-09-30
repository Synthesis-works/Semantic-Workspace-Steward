"""Safe action-planning workflow (M7): pre-execution decision, authorization, ticket.

The workflow turns a *candidate* action for a resource into a deterministic
``ActionPlan``: it runs the rule-based ``ActionAuthorizer`` against the
configured ``ExecutionMode`` and, whenever the authorization gate requires
human approval, creates a ``PENDING`` ticket in the approval store.

M7 boundary — nothing here executes anything:

  - No AWS client, collector, trace recorder, or network code is reachable
    from this module.
  - No ``ActionExecutor`` implementation exists yet; ``ActionPlan.executed``
    is always False.
  - A plan is a record of the pre-execution gate, never a command to run.
  - The LLM is never involved: "AI understands. Deterministic policy decides."

The planner is stateless in derivation: identical inputs always produce
identical plans (the approval store is the only mutated state, and only by
creating a PENDING ticket when the gate requires human approval).
"""

from __future__ import annotations

from ._identity import new_id, utc_now
from .approval import InMemoryApprovalStore
from .authorization import ActionAuthorizer
from .constants import ExecutionMode, PotentialAction, SWSResourceType
from .models import ActionPlan, AuthorizationRequest, PolicyDecision


class ActionPlanner:
    """Deterministic decision -> authorization -> ticket planner (pre-execution).

    Consumes the existing ``ActionAuthorizer`` and ``InMemoryApprovalStore``
    primitives into one reachable workflow: this is the first production
    caller of the authorization gate. The execution mode is an operator
    setting fixed at construction; it is never a per-request client input,
    so a caller cannot widen autonomy.

    Derivation is deterministic: identical inputs always render identical
    authorization decisions. Each returned plan additionally carries a
    stable ``action_plan_id`` and an aware ``created_at`` so the plan is
    uniquely referenceable in the durable audit ledger (M8); both come from
    injectable sources (``now`` / ``plan_id_source``) so tests are
    deterministic. The approval store is the only mutated state, and only
    by creating a PENDING ticket when the gate requires human approval.
    """

    def __init__(
        self,
        *,
        approval_store: InMemoryApprovalStore,
        authorizer: ActionAuthorizer | None = None,
        execution_mode: ExecutionMode = ExecutionMode.SAFE,
        now=None,
        plan_id_source=None,
    ) -> None:
        self._store = approval_store
        self._authorizer = (
            authorizer if authorizer is not None else ActionAuthorizer()
        )
        self._execution_mode = execution_mode
        self._now = now or utc_now
        self._plan_id_source = plan_id_source or new_id

    @property
    def execution_mode(self) -> ExecutionMode:
        """The operator-configured autonomy mode used for authorization."""
        return self._execution_mode

    def plan(
        self,
        *,
        resource_id: str,
        resource_type: SWSResourceType,
        action: PotentialAction,
        rationale: str = "",
        decision: PolicyDecision | None = None,
    ) -> ActionPlan:
        """Authorize one candidate action and open a ticket when required.

        Out-of-vocabulary actions are rejected at ``AuthorizationRequest``
        construction (fail-fast); actions the gate requires human approval
        for produce a new PENDING ticket in the injected approval store.
        Nothing is executed.

        M9 lineage: when the ``PolicyDecision`` that motivated the request is
        supplied, it must belong to the same resource, and its ``decision_id``
        / ``snapshot_id`` / ``run_id`` are stamped onto the plan so the plan
        can be correlated back to the exact policy evaluation and collection
        run in the durable audit ledger. Omitting the decision remains valid
        and leaves those fields unset.
        """
        if decision is not None and decision.resource_id != resource_id:
            raise ValueError("decision.resource_id does not match resource_id")
        request = AuthorizationRequest(
            resource_id=resource_id,
            resource_type=resource_type,
            action=action,
            execution_mode=self._execution_mode,
        )
        result = self._authorizer.authorize(request)
        action_plan_id = self._plan_id_source()
        created_at = self._now()
        ticket = None
        if result.requires_human_approval:
            ticket = self._store.create_ticket(
                resource_id=resource_id,
                action=action,
                rationale=rationale,
                plan_id=action_plan_id,
            )
        return ActionPlan(
            action_plan_id=action_plan_id,
            created_at=created_at,
            resource_id=resource_id,
            action=action,
            execution_mode=self._execution_mode,
            authorization=result,
            ticket=ticket,
            decision_id=decision.decision_id if decision is not None else None,
            snapshot_id=decision.snapshot_id if decision is not None else None,
            run_id=decision.run_id if decision is not None else None,
        )