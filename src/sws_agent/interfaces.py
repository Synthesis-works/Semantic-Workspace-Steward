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


# --- Execution-ledger exception taxonomy -----------------------------------
#
# These live here rather than in the SQLite implementation because a caller
# holding an `ExecutionLedger` must be able to tell its failure modes apart
# without knowing which store it was handed. The coordinator is the case in
# point: it reports three refusals whose operator responses differ, and it
# cannot do that by string-matching a generic error.
#
# Conventions mirror approval.py: one base class per store, with a distinct
# subclass per failure mode a caller may need to tell apart.


class ExecutionLedgerError(Exception):
    """Base class for execution-ledger failures."""


class UnknownReservationError(ExecutionLedgerError):
    """Raised when no reservation exists for the requested pair."""


class ExecutionLedgerUnavailableError(ExecutionLedgerError):
    """Raised when the ledger cannot be opened, written, or committed.

    Covers a closed store, an unwritable path, and a writer that lost the
    race for the database lock. The store never degrades to an in-memory
    implementation or to a partial write.
    """


class ExecutionLedgerCorruptionError(ExecutionLedgerError):
    """Raised when durable execution data fails its integrity checks.

    The store fails closed: it never reconstructs a plausible state from
    damaged data, because a plausible-but-wrong execution record could hide
    a claim that is still live.

    Deliberately *not* a :class:`ReservationConflictError`. A conflict is an
    ordinary answer to "may I execute this?", whereas corruption means the
    ledger cannot answer that question at all. Reporting damaged durable state
    as contention would tell a caller to retry, and retrying on the strength of
    an unverifiable record is exactly what the fail-closed rule forbids.
    """


class ReservationConflictError(ExecutionLedgerError):
    """Base class for "this execution is already claimed" refusals.

    Distinct from :class:`ReservationRevisionConflictError` on purpose, and
    for the same reason ``DuplicateTicketError`` is distinct in approval: a
    conflict means *this identity is taken* and the caller must not act,
    whereas a revision conflict means *the state you meant to act on has
    already moved* and the caller must re-read before deciding. Both are
    refusals to proceed; only one of them is safe to retry blindly, and
    neither is.
    """


class AlreadyReservedError(ReservationConflictError):
    """Raised when a live ``RESERVED`` claim already exists for the pair."""


class AlreadyAttemptedError(ReservationConflictError):
    """Raised when an ``ATTEMPTED`` execution already exists for the pair.

    The external boundary is recorded as crossed, so no other worker may
    claim this execution under any circumstance.
    """


class AlreadyResolvedError(ReservationConflictError):
    """Raised when the execution already reached a terminal state."""


class IntentAlreadyExecutedError(ReservationConflictError):
    """Raised when a prior same-intent execution blocks a fresh ticket.

    Distinct from the pair-scoped conflicts above because the two answer
    different questions. The pair conflicts mean *this authorization instance
    is already claimed*; this means *this effect may already have been
    performed, or may yet be performed, under an earlier authorization*.

    Every prior state of the same ``intent_key`` blocks except a recorded
    ``FAILED``. A ``RESERVED`` claim blocks because M13 has no automatic
    stale-reservation takeover, so the previous worker may still be running.
    An ``ATTEMPTED`` row blocks because the external boundary is recorded as
    crossed. A settled known effect blocks because it demonstrably happened.
    ``UNRESOLVED``/``UNKNOWN`` blocks because nothing established whether it
    happened at all, and that ambiguity must not be laundered into a retry by
    the arrival of a new ticket.

    Only ``FAILED`` -- a known unsuccessful outcome, where a separately
    authorized reattempt is meaningful -- permits a second execution of the
    same intent, and that reservation records ``supersedes_reservation_id``
    so the lineage survives in durable state rather than being inferred from
    two rows sharing an intent key.
    """


class SupersedesIntentMismatchError(ExecutionLedgerError):
    """Raised when a lineage reference names a different intent's execution.

    Lineage exists to answer "why was a second execution of this intent
    permitted". If the referenced reservation belongs to another intent, that
    answer is wrong or absent, and recording it would leave a durable claim
    that cannot be substantiated. Refusing is the only safe response.
    """


class TicketRevisionConflictError(ReservationConflictError):
    """Raised when the pair exists but bound to a different ticket revision.

    A reservation is bound to the exact approval revision it was created
    against, so it cannot be silently reused by a caller holding a
    different view of the same ticket.

    This is a *binding* failure rather than an execution duplicate: the
    approval moved between the reservation and the consumption, so the two
    no longer describe the same authorization. Reporting it as a duplicate
    would send an operator looking for a second worker when the real cause is
    a ticket that changed underneath the reservation.
    """


class ReservationOwnershipError(ReservationConflictError):
    """Raised when a worker tries to transition a reservation it does not hold.

    ``reserve`` binds an execution to one ``worker_id``, and only that worker
    may advance it. Without this gate the binding would be decorative: any
    caller that happened to learn an ``(intent_key, ticket_id)`` pair could
    call ``mark_attempted`` or ``record_outcome`` on it, and could in
    particular stamp a definite outcome onto another worker's execution. A
    reservation's whole purpose is that the claim is exclusive, so the
    transitions that decide what happened must be exclusive too.

    This is a :class:`ReservationConflictError` because the caller's response
    is the same in both cases -- do not proceed -- but it is a distinct type
    because the fault is different: ``AlreadyAttemptedError`` means the
    execution is no longer yours to run, while this means it never was.
    """


class ReservationRevisionConflictError(ExecutionLedgerError):
    """Raised when a transition's expected revision is not the stored one.

    The store guards every state change with a compare-and-swap on
    ``revision`` and never retries silently.
    """


class InvalidExecutionTransitionError(ExecutionLedgerError):
    """Raised when a transition is not in the execution transition table."""


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
  * **Intent-level effect guard.** The pair key names an *authorization
    instance*, not the effect being performed, so it is not by itself
    authority to act. ``reserve`` must additionally decide, atomically in the
    same operation, whether the intent is still executable, and refuse a fresh
    ticket whenever a prior same-intent execution is ``RESERVED``,
    ``ATTEMPTED``, ``UNRESOLVED``, or settled under a basis that records
    repeating the intent as unsafe. The basis, not the outcome word, is the
    test: a settled execution whose recorded basis permits one is permitted and
    must record which prior execution it supersedes. Exposing this as a
    separate read is not sufficient: it would restore a read-then-act race
    between processes, which is the failure this seam exists to prevent.
* **Re-executability is a recorded fact, not an inference.** Settling an
    execution requires the caller to state *why* repeating the intent is or is
    not semantically safe, and the store validates that statement against its
    own compatibility table rather than storing whatever it is given. The
    reason is that ``FAILED`` alone cannot carry the decision: a request that
    never left the process, a request the provider definitively refused as
    invalid for that target, and a request whose effect was never established
    are all "unsuccessful" and only the first may be repeated freely. The
    argument is required rather than defaulted, because a default is how the
    previous ``FAILED``-implies-retryable rule would survive in every call
    site that never thought about it.
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
* **No automatic override.** Refusing an ``UNRESOLVED`` prior is not a
      dead end with a workaround: permitting a second execution of an intent
      whose effect is unknown requires an explicit reconciliation mechanism with
      its own authorization and audit semantics, which M13 deliberately does not
      provide. A new approval ticket is not such a mechanism -- it says nothing
      about whether the earlier effect occurred. The same applies to a settled
      execution whose basis records repeating the intent as unsafe: a fresh
      ticket does not substitute for re-observing the target and re-planning.
    * **Lineage is derived, not supplied.** ``reserve`` takes no parameter
      naming the prior execution to supersede, so a caller cannot point a
      reservation at an arbitrary row. The reference is a consequence of what
      was already durably recorded, and ``verify_lineage`` proves afterwards
      that the stored relations are self-consistent. The lineage query selects
      on the same basis that permitted the reattempt, so a reservation cannot
      point at a row that would have blocked it.

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
        reexecution_class: Any,
        expected_revision: int | None = None,
    ) -> Any: ...

    def open_executions(self) -> tuple[Any, ...]: ...

    def unresolved_executions(self) -> tuple[Any, ...]: ...

    def verify(self) -> None:
        """Fail closed unless durable execution data is internally consistent.

        Part of the contract rather than a diagnostic convenience: a ledger is
        expected to apply this to itself when it opens, so an implementation
        that cannot prove its own contents cannot honour the contract at all.
        It raises :class:`ExecutionLedgerCorruptionError` rather than returning
        a result, and never reconstructs a plausible state from damaged data.
        """
        ...

    def verify_lineage(self) -> None:
        """Prove every recorded supersession relation is well formed.

        ``reserve`` cannot produce a reference to another intent, to a
        non-``FAILED`` execution, or to itself, so this is not a re-check of
        the decision path. It exists for the case that path cannot cover: a
        file edited, restored from an inconsistent backup, or written by a
        build with a different bug.

        A durable authority that cannot prove its own contents is not an
        authority, so this raises on corruption rather than returning a
        result. The walk is bounded, so a cycle is reported as corruption
        instead of looping.
        """
        ...


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