"""Protocol boundaries between SWS orchestration layers.

Structural convention (Protocols for decoupling) is reused from SMS's
interfaces.py; every interface in this module is written fresh for SWS's
AWS workspace domain.

These boundaries keep the MCP layer thin: business logic implements these
Protocols, and the MCP adapter only adapts them to MCP tools and
resources. Policy, authorization, and action logic never live in the MCP
layer.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .constants import PotentialAction
from .models import (
    AnalysisReport,
    ApprovalTicket,
    AuthorizationResult,
    ExplanationResult,
    PolicyDecision,
    ResourceRecord,
)


@runtime_checkable
class TraceSink(Protocol):
    """Append-only execution-event sink."""

    def record(self, event_type: Any, message: str, **kwargs: Any) -> Any: ...
    def succeed(self, event_type: Any, message: str, **kwargs: Any) -> Any: ...
    def fail(self, event_type: Any, message: str, **kwargs: Any) -> Any: ...


@runtime_checkable
class InventoryCollector(Protocol):
    """Collects a bounded inventory of a single AWS resource type.

    Deterministic metadata and metric collection; the collector must
    respect per-request resource limits and pagination.
    """

    def collect(self, *, limit: int | None = None) -> list[ResourceRecord]: ...


@runtime_checkable
class ResourceAnalyzer(Protocol):
    """Produces a semantically framed report for a single resource.

    The semantic layer may explain or interpret; it must not bypass
    policy evaluation or authorization.
    """

    def analyze(self, resource: ResourceRecord) -> AnalysisReport: ...


@runtime_checkable
class PolicyEngine(Protocol):
    """Deterministic policy evaluation.

    Must be independently testable and must never delegate its decision
    to an LLM. "AI understands. Deterministic policy decides."
    """

    def evaluate(self, resource: ResourceRecord) -> PolicyDecision: ...


@runtime_checkable
class ApprovalProvider(Protocol):
    """Human-approval boundary.

    Obtains the required human approval implied by an authorization
    decision and returns the resolved outcome.
    """

    def request_approval(self, decision: PolicyDecision) -> AuthorizationResult: ...


@runtime_checkable
class ApprovalStore(Protocol):
    """Durable, single-use human-approval ticket store (M12).

    The authoritative state of a human approval lives here and nowhere
    else. The M12 investigation found that the pre-M12 arrangement allowed
    the approval state machine and the durable audit ledger to have
    independent lifetimes, so a grant recorded on disk could be silently
    unredeemable after a restart. This protocol is the seam that lets the
    durable implementation replace the in-memory test double without any
    caller changing.

    Contract every implementation must satisfy:

    * **Closed state machine.** Transitions are exactly those in
      ``approval.LEGAL_APPROVAL_TRANSITIONS``:
      ``PENDING -> GRANTED | DENIED | EXPIRED | REVOKED`` and
      ``GRANTED -> CONSUMED | EXPIRED | REVOKED``. ``DENIED``, ``EXPIRED``,
      ``CONSUMED``, and ``REVOKED`` are terminal. Any other request raises
      ``approval.InvalidTransitionError``.
    * **Exactly-once redemption.** ``consume`` may succeed at most once per
      ticket. A CONSUMED ticket is terminal, so a second call raises rather
      than re-marking it.
    * **Monotonic revision.** Every committed transition advances
      ``ticket.revision`` by exactly one. Callers may read it as a
      precondition; whether an implementation enforces compare-and-swap
      against it is a property of the implementation, not of this protocol.
    * **Bounded validity.** A PENDING ticket expires if undecided past the
      decision TTL, and a GRANTED ticket expires if unredeemed past its
      ``execution_deadline``. Both bounds are enforced on read, so an
      implementation must return the terminal state rather than a stale one.
    * **No derivation.** ``create_ticket`` records the intent key and
      evidence digest exactly as supplied. Computing either value is the
      caller's responsibility, so that the identity an approver authorized is
      decided in one place rather than inferred inconsistently.

    Implementations raise ``approval.UnknownTicketError`` for an unknown id.
    Persistence, clocking, and TTL configuration are implementation details
    and are deliberately absent from this surface.
    """

    def create_ticket(
        self,
        resource_id: str,
        action: PotentialAction,
        rationale: str = "",
        ticket_id: str | None = None,
        plan_id: str | None = None,
        execution_intent_key: str | None = None,
        evidence_digest: str | None = None,
    ) -> ApprovalTicket: ...

    def get(self, ticket_id: str) -> ApprovalTicket: ...

    def grant(
        self, ticket_id: str, *, decided_by: str = "", reason: str = ""
    ) -> ApprovalTicket: ...

    def deny(
        self, ticket_id: str, *, decided_by: str = "", reason: str = ""
    ) -> ApprovalTicket: ...

    def expire(
        self, ticket_id: str, *, decided_by: str = "", reason: str = ""
    ) -> ApprovalTicket: ...

    def revoke(
        self, ticket_id: str, *, decided_by: str = "", reason: str = ""
    ) -> ApprovalTicket: ...

    def consume(
        self, ticket_id: str, *, expected_revision: int | None = None
    ) -> ApprovalTicket:
        """Redeem a GRANTED ticket exactly once, optionally pinning a revision.

        ``expected_revision`` is the exact-revision precondition M13 needs. When
        supplied, the transition succeeds only if the ticket is currently at
        exactly that revision, and raises ``RevisionConflictError`` otherwise.
        When omitted, behaviour is unchanged: consume whatever is currently
        ``GRANTED``.

        M13's execution ledger records the approval revision a reservation was
        claimed against. Without this parameter a caller could reserve against
        revision *R*, let the ticket advance, and then redeem the newer
        revision while the execution ledger still asserts the older one -- the
        two stores would disagree about which authorization was used. This
        parameter is what lets a caller force them to agree at the redemption
        boundary. It is deliberately keyword-only and optional so existing
        callers keep working unchanged.
        """
        ...

    def pending(self) -> list[ApprovalTicket]: ...


@runtime_checkable
class ExecutionLedger(Protocol):
    """Durable, exclusive claim on a single execution (M13 Phase 3).

    The authoritative record of *which executions have been claimed* lives
    here and nowhere else. This is deliberately a different authority from
    :class:`ApprovalStore`: approval answers "is this action authorized?",
    and this answers "has this execution already been claimed, was it
    attempted, and what is known about what happened?".

    The M13 Phase 2 investigation established why the two must stay
    separate. The approval compare-and-swap protects the *approval record*
    while leaving the *external side effect* unprotected -- measured against
    the real durable approval store, four independent processes all crossed
    the external-effect boundary and only one later won ``consume``. The
    audit ledger cannot fill the gap either: its duplicate scans answer from
    an in-memory dict populated once at open, so they are process-local by
    construction, and concurrent appends lose records on this platform.

    Contract every implementation must satisfy:

    * **Exactly-once claim.** ``reserve`` may succeed at most once per
      ``(intent_key, ticket_id)``. A second call raises rather than
      re-claiming, and the caller must not proceed to any external effect.
    * **Read current state.** Every operation reads durable state. An
      implementation must not answer from a cache populated at open, because
      that is precisely the failure this seam exists to close.
    * **Monotonic revision.** Every committed transition advances
      ``revision`` by exactly one, and a caller-supplied expected revision
      is honoured as a precondition.
    * **Transitions are bound to the claiming worker.** ``mark_attempted``
      and ``record_outcome`` require the ``worker_id`` that made the
      reservation. Otherwise any caller that learned a pair could stamp a
      definite outcome onto another worker's execution, which would make the
      exclusive claim meaningless.
    * **No automatic release.** A reservation that may have crossed the
      external boundary is never released for another worker, and an
      unresolved outcome is never retried automatically. Neither is there
      an automatic takeover of an abandoned claim: a worker that crashed
      before its external call and one that crashed during it leave
      identical durable state.
    * **No approval authority.** This seam reads ``ticket_id`` and
      ``ticket_revision`` only as opaque bindings. It never consumes an
      approval and never decides whether an action is authorized.

    Implementations raise ``execution_ledger.UnknownReservationError`` for an
    unclaimed pair and a ``ReservationConflictError`` subclass when the pair
    is already claimed. Persistence, clocking, and locking are implementation
    details and are deliberately absent from this surface.
    """

    def reserve(
        self,
        *,
        intent_key: str,
        ticket_id: str,
        ticket_revision: int,
        worker_id: str,
        action_plan_id: str | None = None,
        resource_id: str | None = None,
        action: PotentialAction | None = None,
    ) -> Any: ...

    def get(self, intent_key: str, ticket_id: str) -> Any: ...

    def mark_attempted(
        self,
        intent_key: str,
        ticket_id: str,
        *,
        worker_id: str,
        expected_revision: int | None = None,
    ) -> Any: ...

    def record_outcome(
        self,
        intent_key: str,
        ticket_id: str,
        outcome: Any,
        *,
        worker_id: str,
        expected_revision: int | None = None,
    ) -> Any: ...

    def open_executions(self) -> tuple[Any, ...]: ...

    def unresolved_executions(self) -> tuple[Any, ...]: ...


@runtime_checkable
class ActionExecutor(Protocol):
    """Executes a narrowly scoped action and returns the actual outcome.

    Execution must follow: validate target -> validate eligibility ->
    deterministic policy -> approval -> execute -> verify resulting AWS
    state -> record actual outcome. This foundation defines the boundary
    only; no AWS actions are implemented yet.
    """

    def execute(self, resource: ResourceRecord, action: Any) -> Any: ...


@runtime_checkable
class ExplanationProvider(Protocol):
    """Optional natural-language explanation of a resource or decision.

    SWS must function fully when no explanation provider is registered;
    the default NullExplanationProvider returns an ExplanationResult with
    ``text=None``. Explanations are interpreted claims only and never
    influence policy, authorization, or risk decisions.
    """

    def explain(
        self, resource: ResourceRecord, decision: PolicyDecision
    ) -> ExplanationResult: ...