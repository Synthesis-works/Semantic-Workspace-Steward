"""Minimal in-memory human-approval ticket store.

Human approval is the boundary for future side-effecting actions. This
module models approval tickets and their state machine only: it performs
no persistence, no AWS calls, and no action execution. Time is injectable
so TTL expiration is deterministically testable without sleeping.

M12 legal transitions (enforced by the store):

    PENDING -> GRANTED
    PENDING -> DENIED
    PENDING -> EXPIRED
    PENDING -> REVOKED
    GRANTED -> CONSUMED
    GRANTED -> EXPIRED   (only once the execution deadline has passed)
    GRANTED -> REVOKED

DENIED, EXPIRED, CONSUMED, and REVOKED are terminal. Two independent
time bounds apply to a ticket, and M12 separates them:

* the *decision TTL* bounds how long a ticket may await a human decision;
* the *execution deadline* bounds how long a granted ticket may authorize a
  mutation. M9 had no second bound, so a grant stayed valid for the entire
  remaining life of the process.

M9 also modelled consumption as an orthogonal ``consumed`` boolean on a
still-``GRANTED`` ticket. M12 promotes it to the first-class ``CONSUMED``
status so a redeemed approval is distinguishable from a live one; the
``consumed`` field survives only as a validated mirror.

This store is a deterministic test double. M12 Phase 3 adds a durable,
transactionally serializable implementation of the same contract; the
``interfaces.ApprovalStore`` protocol is the boundary both satisfy.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Final

from .constants import ApprovalStatus, PotentialAction
from .models import ApprovalTicket

DEFAULT_APPROVAL_TICKET_TTL: Final[timedelta] = timedelta(hours=24)
"""Default time-to-live for a pending approval ticket.

Bounds the *decision* window only: how long a ticket may remain PENDING
before it expires. Unchanged from M9.
"""

DEFAULT_APPROVAL_EXECUTION_TTL: Final[timedelta] = timedelta(hours=1)
"""Default validity window for a granted approval's *execution*.

**This is an M12 implementation default, not a product decision.** The M12
investigation showed that M9 granted approvals never expired, which is
fail-open in the wrong direction: a single approval could authorize a
mutation arbitrarily long after the human gave it. The fix had to pick some
bound, and one hour was chosen as the conservative placeholder so that a
grant cannot silently outlive a working session.

The value is deliberately injected (``InMemoryApprovalStore(execution_ttl=...)``)
so it can be changed without touching this constant. Treat it as a
placeholder pending an explicit product decision on how long a human
approval should remain redeemable.
"""

TERMINAL_APPROVAL_STATUSES: Final[frozenset[ApprovalStatus]] = frozenset(
    {
        ApprovalStatus.DENIED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CONSUMED,
        ApprovalStatus.REVOKED,
    }
)
"""Approval states that admit no outgoing transition."""

LEGAL_APPROVAL_TRANSITIONS: Final[
    dict[ApprovalStatus, frozenset[ApprovalStatus]]
] = {
    ApprovalStatus.PENDING: frozenset(
        {
            ApprovalStatus.GRANTED,
            ApprovalStatus.DENIED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.REVOKED,
        }
    ),
    ApprovalStatus.GRANTED: frozenset(
        {
            ApprovalStatus.CONSUMED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.REVOKED,
        }
    ),
    ApprovalStatus.DENIED: frozenset(),
    ApprovalStatus.EXPIRED: frozenset(),
    ApprovalStatus.CONSUMED: frozenset(),
    ApprovalStatus.REVOKED: frozenset(),
}
"""The complete M12 approval transition table.

``GRANTED -> EXPIRED`` is reachable only through deadline enforcement, which
the store performs lazily on read and before every transition; a caller
cannot force it at an arbitrary time.
"""


class ApprovalError(Exception):
    """Base class for approval-store failures."""


class UnknownTicketError(ApprovalError):
    """Raised when a ticket id is not present in the store."""


class InvalidTransitionError(ApprovalError):
    """Raised when a transition is not in the M12 approval transition table.

    M9 could only transition out of PENDING, so this once meant exactly
    "already decided". M12 allows a second step from GRANTED, so a caller may
    now hit this for a genuine state-machine violation as well as for a
    repeat decision.
    """


class DuplicateTicketError(ApprovalError):
    """Raised when a caller-supplied ``ticket_id`` already exists.

    Distinct from :class:`RevisionConflictError` on purpose. A duplicate means
    "this identity is taken", which a caller can resolve by choosing another
    id. A revision conflict means "the state you meant to act on has already
    moved", which the caller must re-read before deciding.
    """


class RevisionConflictError(ApprovalError):
    """Raised when a transition's expected revision is not the stored one.

    The durable store guards every mutation with a compare-and-swap on
    ``revision``. When the precondition fails the store refuses the write and
    reports this rather than overwriting newer approval state, and never
    retries silently.
    """


class ApprovalStoreUnavailableError(ApprovalError):
    """Raised when the durable store cannot be opened, written, or committed.

    Covers a closed store, an unwritable path, and a writer that lost the
    race for the database lock. The store never degrades to an in-memory
    implementation or to a partial write when this happens.
    """


class ApprovalStoreCorruptionError(ApprovalError):
    """Raised when durable approval data fails its integrity checks.

    The store fails closed: it never reconstructs a plausible state from
    damaged data, because a plausible-but-wrong approval is more dangerous
    than an unavailable one.
    """


class InMemoryApprovalStore:
    """Deterministic, in-memory approval ticket state machine (M12).

    The store is not durable: it is the test double for the
    ``interfaces.ApprovalStore`` contract. An injected clock plus the two
    TTLs make expiration deterministic and testable without sleeping.

    Every committed transition advances ``ticket.revision`` by exactly one.
    The in-memory store deliberately performs **no** compare-and-swap on
    ``revision``: the GIL makes a dict rebind effectively atomic here, and
    M12 Phase 3 is where a real transactional CAS is introduced. Recording
    the revision now means the durable store has a precondition to check
    rather than having to invent one later.

    Expiration is lazy. A ticket past a bound is flipped to ``EXPIRED`` on
    the next read or transition, which keeps the state machine pure with
    respect to the injected clock.
    """

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_APPROVAL_TICKET_TTL,
        execution_ttl: timedelta = DEFAULT_APPROVAL_EXECUTION_TTL,
    ) -> None:
        self._now: Callable[[], datetime] = now or (
            lambda: datetime.now(timezone.utc)
        )
        self._ttl = ttl
        self._execution_ttl = execution_ttl
        self._tickets: dict[str, ApprovalTicket] = {}

    def create_ticket(
        self,
        resource_id: str,
        action: PotentialAction,
        rationale: str = "",
        ticket_id: str | None = None,
        plan_id: str | None = None,
        execution_intent_key: str | None = None,
        evidence_digest: str | None = None,
    ) -> ApprovalTicket:
        """Issue a new PENDING ticket at ``revision`` 0.

        ``execution_intent_key`` and ``evidence_digest`` are recorded exactly
        as supplied. Phase 1 does not derive either value; stamping the
        intent key from the plan and defining the evidence digest are Phase 5
        concerns, so both stay ``None`` on every planner-created ticket.
        """
        ticket = ApprovalTicket(
            ticket_id=ticket_id or uuid.uuid4().hex,
            resource_id=resource_id,
            action=action,
            rationale=rationale,
            created_at=self._now(),
            plan_id=plan_id,
            execution_intent_key=execution_intent_key,
            evidence_digest=evidence_digest,
        )
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def get(self, ticket_id: str) -> ApprovalTicket:
        self._require_known(ticket_id)
        self._expire_if_stale(ticket_id)
        return self._tickets[ticket_id]

    def grant(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
    ) -> ApprovalTicket:
        """PENDING -> GRANTED, stamping the execution deadline.

        M12: the deadline is set at grant time, not at issue time, because
        the window a human authorizes is the window that begins when they say
        yes. ``decided_at`` and the deadline share the same injected instant.
        """
        return self._resolve(
            ticket_id, ApprovalStatus.GRANTED, decided_by, reason
        )

    def deny(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
    ) -> ApprovalTicket:
        return self._resolve(
            ticket_id, ApprovalStatus.DENIED, decided_by, reason
        )

    def expire(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
    ) -> ApprovalTicket:
        return self._resolve(
            ticket_id, ApprovalStatus.EXPIRED, decided_by, reason
        )

    def revoke(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
    ) -> ApprovalTicket:
        """Withdraw a PENDING or GRANTED ticket.

        M12: revocation did not exist before, so an approval that had already
        been granted could never be taken back; the only remaining options
        were to redeem it or to let the process die. A REVOKED ticket is
        terminal and can never authorize an attempt.
        """
        return self._resolve(
            ticket_id, ApprovalStatus.REVOKED, decided_by, reason
        )

    def consume(self, ticket_id: str) -> ApprovalTicket:
        """Redeem a GRANTED ticket exactly once (M9, restated in M12).

        Transitions GRANTED -> CONSUMED. A consumed ticket is terminal, so a
        second redemption raises ``InvalidTransitionError``; the store cannot
        mark a CONSUMED ticket as consumed again, because the status no longer
        admits an outgoing transition. Anything that is not currently
        GRANTED (PENDING, DENIED, EXPIRED, CONSUMED, REVOKED) is refused.

        The transition is deliberately not atomic with respect to the caller's
        mutation. ``ExecutionCoordinator`` still consumes *after* the handler
        runs (see ``execution.py``); reordering that is M13, not M12.
        """
        self._require_known(ticket_id)
        self._expire_if_stale(ticket_id)
        return self._commit(
            ticket_id,
            ApprovalStatus.CONSUMED,
            decided_by="",
            reason="",
        )

    def pending(self) -> list[ApprovalTicket]:
        """Return tickets still awaiting approval.

        Tickets past the decision TTL are expired first, so expired tickets
        are excluded from the result deterministically. REVOKED and DENIED
        tickets are likewise absent: only PENDING work is outstanding.
        """
        for ticket_id in list(self._tickets):
            self._expire_if_stale(ticket_id)
        return [
            ticket
            for ticket in self._tickets.values()
            if ticket.status is ApprovalStatus.PENDING
        ]

    def _resolve(
        self,
        ticket_id: str,
        new_status: ApprovalStatus,
        decided_by: str,
        reason: str,
    ) -> ApprovalTicket:
        self._require_known(ticket_id)
        self._expire_if_stale(ticket_id)
        return self._commit(ticket_id, new_status, decided_by, reason)

    def _commit(
        self,
        ticket_id: str,
        new_status: ApprovalStatus,
        decided_by: str,
        reason: str,
    ) -> ApprovalTicket:
        """Validate a transition against the M12 table and commit it.

        The successor ticket is rebuilt through ``model_validate`` rather than
        ``model_copy`` so the model's status/consumed invariant is enforced on
        every transition: a bug in a caller could not produce a CONSUMED
        ticket that still reports ``consumed=False``.

        ``decided_at`` records when the ticket was *decided* and is written
        only once, on the first transition. M9 had a single decision point so
        the distinction did not arise; M12 adds transitions that happen after
        a decision (redemption, later revocation, deadline expiry), and
        stamping them would silently move ``decided_at`` off the moment the
        human actually answered. ``decision_reason`` is a different field: it
        describes the ticket's *current* state, so a later transition that
        carries a reason replaces the previous one. Redemption passes no
        reason, which keeps the human's stated reason intact on a spent
        approval; the full history remains in the append-only audit ledger,
        which already records a TICKET payload per transition.
        """
        ticket = self._tickets[ticket_id]
        permitted = LEGAL_APPROVAL_TRANSITIONS[ticket.status]
        if new_status not in permitted:
            allowed = ", ".join(sorted(s.value for s in permitted)) or "none"
            raise InvalidTransitionError(
                f"ticket '{ticket_id}' cannot transition from "
                f"{ticket.status.value} to {new_status.value} "
                f"(legal targets from {ticket.status.value}: {allowed})"
            )
        moment = self._now()
        changes: dict[str, object] = {
            "status": new_status,
            "consumed": new_status is ApprovalStatus.CONSUMED,
            "revision": ticket.revision + 1,
        }
        if ticket.decided_at is None:
            changes["decided_at"] = moment
        if new_status is ApprovalStatus.GRANTED:
            changes["execution_deadline"] = moment + self._execution_ttl
        if decided_by:
            changes["decided_by"] = decided_by
        if reason:
            changes["decision_reason"] = reason
        updated = ApprovalTicket.model_validate(
            {**ticket.model_dump(), **changes}
        )
        self._tickets[ticket_id] = updated
        return updated

    def _require_known(self, ticket_id: str) -> None:
        if ticket_id not in self._tickets:
            raise UnknownTicketError(f"unknown approval ticket: {ticket_id}")

    def _expire_if_stale(self, ticket_id: str) -> None:
        """Lazily enforce both time bounds (M12).

        M9 enforced only the decision TTL and only for PENDING tickets, so a
        GRANTED ticket could never expire. M12 checks each bound against the
        state it actually applies to:

        * PENDING past ``created_at + ttl`` expires on the decision window;
        * GRANTED past ``execution_deadline`` expires on the execution window.

        Both transitions are committed through ``_commit`` so ``revision``
        advances and the status/consumed invariant holds.
        """
        ticket = self._tickets[ticket_id]
        moment = self._now()
        if ticket.status is ApprovalStatus.PENDING:
            if moment - ticket.created_at <= self._ttl:
                return
            self._commit(
                ticket_id,
                ApprovalStatus.EXPIRED,
                decided_by="",
                reason=f"ticket expired after {self._ttl} without a decision",
            )
            return
        if ticket.status is ApprovalStatus.GRANTED:
            deadline = ticket.execution_deadline
            if deadline is None or moment <= deadline:
                return
            self._commit(
                ticket_id,
                ApprovalStatus.EXPIRED,
                decided_by="",
                reason=(
                    f"granted approval expired after its "
                    f"{self._execution_ttl} execution window "
                    f"at {deadline.isoformat()}"
                ),
            )
