"""Durable execution reservation and transaction state (M13 Phase 3).

This module is the execution side's answer to a question the approval side
does not answer. The two are deliberately separate authorities:

* :mod:`sws_agent.approval_ledger` answers **"is this action authorized?"**
  and its authority is the human approval ticket.
* this module answers **"has this execution already been claimed, was it
  attempted, and what is known about what happened?"**

Why they cannot be the same store
---------------------------------

M13 Phase 2 measured the existing system and found that the approval CAS
protects the *approval record* while leaving the *external side effect*
unprotected: with four independent processes against the real
``DurableApprovalStore``, all four observed a live ticket and all four
crossed the external-effect boundary, and only one later won ``consume()``.
Exactly-once redemption of an approval is therefore not exactly-once
execution, and no arrangement of approval state alone can make it so: the
AWS call sits outside every local transaction.

Folding reservation into the approval ledger would also break a
load-bearing invariant. ``approval_ledger.verify`` checks that every ticket
carries exactly ``revision + 1`` contiguous events, which is what makes
"state committed but event missing" detectable. A
reservation is not a transition in the approval lifecycle
(``PENDING -> GRANTED -> CONSUMED | EXPIRED | REVOKED``), so it can be
neither added to that event stream nor smuggled into it without weakening
that check. A reservation is a different lifecycle and gets its own store.

The audit ledger is explicitly **not** the coordination authority either.
Phase 2 measured ``JsonlAuditStore`` across genuine processes: 169 of 720
records were lost with every child exiting 0, because ``O_APPEND`` provides
no atomicity on this platform and ``fsync`` does not substitute for it.
Worse, ``records()`` answers from an in-memory dict populated once at open
(``audit.py:192, 270-274``), so its duplicate scans are process-local by
construction. Audit remains evidentiary. Reservation needs a transaction.

Reservation key semantics
-------------------------

Uniqueness is on ``(intent_key, ticket_id)``, where ``intent_key`` is the
existing ``execution_intent_key`` over ``(snapshot_id, resource_id, action)``.

``ticket_id`` is deliberately **not** folded into the hash. Phase 2 measured
the consequence of keying on intent alone: the same
snapshot/resource/action yields an identical intent key across a revoked
ticket and its replacement, so a reservation keyed only on ``intent_key``
would permanently block a legitimately re-approved retry. Keeping
authorization identity as a second dimension of the key means the same
logical intent under a *new* approval is a new authorization instance,
while the same intent under the same approval is always the same
execution. A different snapshot is a different logical intent and is
distinct either way.

State machine
-------------

::

    RESERVED  --mark_attempted-->  ATTEMPTED
    RESERVED  --record_outcome-->  RESOLVED | UNRESOLVED   (refused before the boundary)
    ATTEMPTED --record_outcome-->  RESOLVED | UNRESOLVED

``RESERVED`` and ``ATTEMPTED`` are open; ``RESOLVED`` and ``UNRESOLVED``
are terminal.

The distinction that carries the safety weight is between "reserved" and
"attempted", because it is exactly the distinction between a worker that
provably never called AWS and one whose fate is unknown:

* ``RESERVED`` -- claimed; the external boundary is not recorded as
  crossed.
* ``ATTEMPTED`` -- the boundary was crossed, **or may have been**. A crash
  immediately after the call leaves this state, indistinguishable from a
  completed call.
* ``RESOLVED`` -- a definite ``ExecutionOutcome`` was recorded.
* ``UNRESOLVED`` -- the outcome could not be established. This is terminal
  and is never automatically retried.

Terminal states admit no outgoing transition, so ``UNRESOLVED`` cannot be
retried by any automated path. Resolving it needs an operator, and the
transition that an operator would use is deliberately **not** defined here:
an automatic resolution rule is exactly the thing that would be unsafe, so
the next phase that introduces reconciliation must decide it explicitly.

Staleness is a report, not a state
----------------------------------

A reservation that outlives a configurable age is *reportable* as stale,
but staleness is deliberately **not** a transition. Phase 2 established
that no automatic takeover is safe: a worker that crashes before its call
and one that crashes during it leave identical durable state, so the
information needed to justify a takeover does not exist at crash time. A
lease that could expire a ``RESERVED`` row into a reusable state would
manufacture exactly the duplicate external effect this module exists to
prevent. Staleness is therefore a read-only property
(:attr:`ExecutionReservation.is_stale`) computed from ``updated_at``, and
the uniqueness constraint keeps holding the row regardless of its age.

Durability and scope
--------------------

Storage follows ``approval_ledger``'s proven discipline: stdlib ``sqlite3``
only, a fresh connection per operation, ``isolation_level=None``,
``BEGIN IMMEDIATE`` for every state change, ``synchronous=FULL``, an
explicit ``busy_timeout``, and fail-closed corruption handling with no
silent repair. ``reserve`` is a single ``BEGIN IMMEDIATE`` transaction, so
exactly one concurrent process can win; the ``UNIQUE`` constraint is the
database-level backstop for the case where two processes somehow both
reach the insert.

This module does **not** consume approvals, does not call AWS, and does not
implement a mutation handler. ``ExecutionCoordinator`` does not reference
it yet; it is an isolated, independently tested primitive.
"""

from __future__ import annotations

import enum
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Final
from uuid import uuid4

from .constants import ExecutionOutcome, PotentialAction

EXECUTION_LEDGER_SCHEMA_VERSION: Final[int] = 1
"""Schema version of the durable execution ledger.

Bumped only by an explicit migration. Opening a ledger whose recorded
version differs is a hard failure: silently reading a layout this build
does not understand is how a durable store starts lying about which
executions were claimed.
"""

DEFAULT_EXECUTION_BUSY_TIMEOUT_SECONDS: Final[float] = 5.0
"""How long a writer waits for SQLite's write lock before failing loudly."""

DEFAULT_EXECUTION_STALE_AFTER: Final[timedelta] = timedelta(hours=1)
"""Reporting-only age past which a reservation may be *reported* as stale.

**This is a reporting threshold, never a takeover lease.** Exceeding it does
not free the reservation, change its state, or permit any other worker to
claim the same execution; the uniqueness constraint continues to hold. The
value exists so an operator can be told "this looks abandoned", not so a
machine can decide "therefore I may act". An automatic takeover would have
to be correct about a crash that happened *during* an external call, and no
durable state distinguishes that case from a crash just before it.
"""


class ExecutionReservationState(str, enum.Enum):
    """Lifecycle of one claimed execution.

    Derived from vocabulary SWS already owns -- ``ExecutionStage`` supplies
    the ATTEMPTED boundary and ``ExecutionOutcome.UNKNOWN`` supplies the
    meaning of UNRESOLVED -- rather than introducing a parallel vocabulary.
    """

    RESERVED = "reserved"
    """Claimed. The external boundary is not recorded as crossed."""

    ATTEMPTED = "attempted"
    """The boundary was crossed, or may have been; the outcome is open."""

    RESOLVED = "resolved"
    """A definite :class:`ExecutionOutcome` was recorded. Terminal."""

    UNRESOLVED = "unresolved"
    """The outcome could not be established. Terminal, never auto-retried."""


LEGAL_EXECUTION_TRANSITIONS: Final[
    dict[ExecutionReservationState, frozenset[ExecutionReservationState]]
] = {
    ExecutionReservationState.RESERVED: frozenset(
        {
            ExecutionReservationState.ATTEMPTED,
        }
    ),
    ExecutionReservationState.ATTEMPTED: frozenset(
        {
            ExecutionReservationState.RESOLVED,
            ExecutionReservationState.UNRESOLVED,
        }
    ),
    ExecutionReservationState.RESOLVED: frozenset(),
    ExecutionReservationState.UNRESOLVED: frozenset(),
}
"""The complete execution transition table.

``RESERVED -> ATTEMPTED -> (RESOLVED | UNRESOLVED)`` is the whole lifecycle,
and the shape is deliberate: reaching a terminal state requires passing
through ``ATTEMPTED``. There is no ``RESERVED -> RESOLVED`` edge, even for an
honest pre-boundary closure such as a refusal or a validation failure.

Allowing it would cost the one property a future reconciliation layer most
needs. ``may_have_crossed_boundary`` is derived from state alone, so it is
only sound if every non-``RESERVED`` state implies the boundary was recorded
as possibly crossed. With a direct edge, a ``RESERVED -> RESOLVED`` row and a
``ATTEMPTED -> RESOLVED`` row would be indistinguishable, and the ledger would
have to claim the former might have called AWS when it provably did not. A
claim abandoned before the boundary instead stays ``RESERVED``, where
:attr:`ExecutionReservation.is_stale` reports it to an operator without
releasing it -- and releasing it is never this store's decision to make, since
an abandoned claim and a crashed-mid-call claim are indistinguishable here.

``UNRESOLVED`` has no outgoing edge, which is what makes it terminal for
automated execution. An operator-driven resolution is a future decision and
is deliberately not encoded here.
"""

TERMINAL_EXECUTION_STATES: Final[frozenset[ExecutionReservationState]] = (
    frozenset(
        {
            ExecutionReservationState.RESOLVED,
            ExecutionReservationState.UNRESOLVED,
        }
    )
)
"""Execution states that admit no outgoing transition.

Membership is what makes ``record_outcome`` refuse on an already-resolved
execution and what makes a duplicate ``reserve`` against a settled execution
an error rather than a silent no-op.
"""

_OPEN_STATES: Final[frozenset[ExecutionReservationState]] = frozenset(
    set(ExecutionReservationState) - TERMINAL_EXECUTION_STATES
)


# --- Errors -----------------------------------------------------------------
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


class TicketRevisionConflictError(ReservationConflictError):
    """Raised when the pair exists but bound to a different ticket revision.

    A reservation is bound to the exact approval revision it was created
    against, so it cannot be silently reused by a caller holding a
    different view of the same ticket.
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


# --- Record -----------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionReservation:
    """One claimed execution, as stored.

    Frozen and value-compared on purpose: a reservation handed to a caller
    must not be mutable in that caller's hands, and ``revision`` is only
    meaningful as the value that was durably observed.
    """

    reservation_id: str
    intent_key: str
    ticket_id: str
    ticket_revision: int
    state: ExecutionReservationState
    revision: int
    worker_id: str
    created_at: datetime
    updated_at: datetime
    action_plan_id: str | None = None
    resource_id: str | None = None
    action: PotentialAction | None = None
    outcome: ExecutionOutcome | None = None
    stale_after: timedelta = DEFAULT_EXECUTION_STALE_AFTER

    @property
    def is_open(self) -> bool:
        """True while the execution is neither resolved nor unresolved."""
        return self.state in _OPEN_STATES

    @property
    def is_terminal(self) -> bool:
        """True once the execution can no longer change state."""
        return self.state in TERMINAL_EXECUTION_STATES

    @property
    def may_have_crossed_boundary(self) -> bool:
        """True once the external boundary is recorded as possibly crossed.

        This is the single predicate a future reconciliation layer should
        gate on: it is true for ``ATTEMPTED`` and stays true through the
        terminal states, so an ``UNRESOLVED`` execution can never be
        mistaken for one that provably never called AWS.
        """
        return self.state is not ExecutionReservationState.RESERVED

    def is_stale(self, as_of: datetime | None = None) -> bool:
        """True when no state change has happened within ``stale_after``.

        **Reporting only.** This never mutates state, never frees the claim,
        and never authorises another worker. It exists so an operator can be
        shown executions that look abandoned.

        ``as_of`` makes the question answerable against the same clock the
        store wrote with. Comparing ``updated_at`` to the wall clock instead
        would make every row look abandoned the moment the ledger is opened
        with a clock of its own -- and would make the property untestable
        without waiting an hour.
        """
        moment = as_of or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("as_of must be a timezone-aware datetime")
        return self.updated_at + self.stale_after <= moment


_RESERVATION_COLUMNS: Final[tuple[str, ...]] = (
    "reservation_id",
    "intent_key",
    "ticket_id",
    "ticket_revision",
    "action_plan_id",
    "resource_id",
    "action",
    "worker_id",
    "state",
    "outcome",
    "revision",
    "created_at",
    "updated_at",
)

_REQUIRED_TABLES: Final[tuple[str, ...]] = ("meta", "execution_reservation")
_EMPTY_SCHEMA_RETRIES: Final[int] = 50
_EMPTY_SCHEMA_RETRY_SECONDS: Final[float] = 0.02

_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_reservation (
        reservation_id TEXT PRIMARY KEY,
        intent_key     TEXT NOT NULL,
        ticket_id      TEXT NOT NULL,
        ticket_revision INTEGER NOT NULL,
        action_plan_id TEXT,
        resource_id    TEXT,
        action         TEXT,
        worker_id      TEXT NOT NULL,
        state          TEXT NOT NULL,
        outcome        TEXT,
        revision       INTEGER NOT NULL,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        UNIQUE (intent_key, ticket_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS execution_reservation_by_state
        ON execution_reservation (state)
    """,
)
"""Schema DDL, applied one statement at a time.

Deliberately not a single ``executescript`` call, for the reason
``approval_ledger`` gives: ``executescript`` implicitly commits any open
transaction before it runs, which would silently end the ``BEGIN
IMMEDIATE`` that makes schema creation race-free against a second process
opening the same file for the first time.

``UNIQUE (intent_key, ticket_id)`` is the database-level guarantee behind
Part 12's invariant: once a reservation exists for a pair, no second
reservation for that pair can exist, no matter how many processes race and
regardless of what any application-level check concluded first. Every
other constraint here is for query shape or operator legibility; this one
is load-bearing.

There is deliberately no event table. The approval ledger needs one because
it commits two related rows per transition and must prove they landed
together; this ledger commits exactly one row per transaction, so
SQLite's own atomicity is the guarantee, and the append-only history of
stages belongs to the audit ledger rather than being duplicated here.
"""


class DurableExecutionLedger:
    """Append-only-by-transition durable execution reservation store.

    The store is the authoritative record of which executions have been
    claimed. It answers three questions the approval ledger cannot:

    * has this ``(intent_key, ticket_id)`` already been claimed?
    * did the external boundary get crossed for it?
    * what is known about what happened?

    It never answers "is this authorized?" -- that is the approval ledger's
    question, and this store reads ``ticket_id`` and ``ticket_revision``
    only as opaque bindings it never validates against the approval store.

    On construction the ledger is verified: SQLite's own ``integrity_check``
    must pass, the recorded schema version must match, and every stored row
    must round-trip as an :class:`ExecutionReservation`. Any failure raises
    :class:`ExecutionLedgerCorruptionError`. The store never reconstructs
    a plausible-looking state from damaged data.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
        id_source: Callable[[], str] | None = None,
        busy_timeout_seconds: float = DEFAULT_EXECUTION_BUSY_TIMEOUT_SECONDS,
        stale_after: timedelta = DEFAULT_EXECUTION_STALE_AFTER,
        verify_on_open: bool = True,
    ) -> None:
        self._path = Path(path)
        self._now: Callable[[], datetime] = now or (
            lambda: datetime.now(timezone.utc)
        )
        self._id_source: Callable[[], str] = id_source or (lambda: uuid4().hex)
        self._busy_timeout = busy_timeout_seconds
        self._stale_after = stale_after
        self._verify_on_open = verify_on_open
        self._closed = False
        if self._now().tzinfo is None:
            raise ValueError(
                "execution ledger clock must return timezone-aware datetimes"
            )
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        if busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds must be positive")
        # Snapshot this *before* connecting: sqlite3 creates an empty file on
        # connect, so deciding "is this new?" afterwards would always say no.
        self._preexisting = self._path.exists() and self._path.stat().st_size > 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if verify_on_open:
            self.verify()

    # -- public API --------------------------------------------------------

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
    ) -> ExecutionReservation:
        """Claim ``(intent_key, ticket_id)`` for exactly one worker.

        This is the only operation that creates state, and it is atomic:
        the read, the absence check, and the insert all happen inside one
        ``BEGIN IMMEDIATE`` transaction, so two processes contending for the
        same pair serialize at the database and exactly one inserts. The
        ``UNIQUE (intent_key, ticket_id)`` constraint is the backstop for the
        case where a second insert is somehow reached anyway; that path
        reports :class:`AlreadyReservedError` rather than letting a raw
        ``sqlite3`` error escape.

        A caller that loses this race receives a
        :class:`ReservationConflictError` and **must not** proceed to any
        external effect. ``ticket_revision`` binds the claim to the exact
        approval revision it was created against; re-reserving the same pair
        with a different revision is a
        :class:`TicketRevisionConflictError`, not a fresh claim, because the
        authorization instance it names is not the one already recorded.
        """
        if not intent_key:
            raise ValueError("intent_key is required to reserve an execution")
        if not ticket_id:
            raise ValueError("ticket_id is required to reserve an execution")
        if not worker_id:
            raise ValueError("worker_id is required to reserve an execution")
        if ticket_revision < 0:
            raise ValueError("ticket_revision must be a non-negative integer")

        reservation_id = self._id_source()
        if not reservation_id:
            raise ExecutionLedgerUnavailableError(
                f"execution ledger {self._path} produced an empty reservation id"
            )
        moment = self._now()
        with self._transaction() as conn:
            existing = self._select_pair(conn, intent_key, ticket_id)
            if existing is not None:
                self._raise_conflict_for(existing, ticket_revision)
            try:
                conn.execute(
                    "INSERT INTO execution_reservation ("
                    "reservation_id, intent_key, ticket_id, ticket_revision, "
                    "action_plan_id, resource_id, action, worker_id, state, "
                    "outcome, revision, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reservation_id,
                        intent_key,
                        ticket_id,
                        ticket_revision,
                        action_plan_id,
                        resource_id,
                        action.value if action is not None else None,
                        worker_id,
                        ExecutionReservationState.RESERVED.value,
                        None,
                        0,
                        _format_ts(moment),
                        _format_ts(moment),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The uniqueness backstop. Something else committed a claim
                # for this pair, so report it in this module's vocabulary.
                self._raise_conflict_for_uncommitted(
                    conn, intent_key, ticket_id, ticket_revision, exc
                )
                raise AssertionError("unreachable")  # pragma: no cover
            row = self._select(conn, reservation_id)
        assert row is not None  # noqa: S101 - inserted in this transaction
        return self._row_to_reservation(row)

    def get(
        self, intent_key: str, ticket_id: str
    ) -> ExecutionReservation:
        """Return the reservation for a pair, or raise if there is none.

        Reads current durable state on every call. There is deliberately no
        cache: Phase 2 showed that an in-memory view populated once at open
        cannot see a concurrent writer, which is precisely the failure this
        store exists to eliminate.
        """
        with self._connection() as conn:
            row = self._select_pair(conn, intent_key, ticket_id)
        if row is None:
            raise UnknownReservationError(
                f"no execution reservation for intent {intent_key!r} and "
                f"ticket {ticket_id!r}"
            )
        return self._row_to_reservation(row)

    def mark_attempted(
        self,
        intent_key: str,
        ticket_id: str,
        *,
        worker_id: str,
        expected_revision: int | None = None,
    ) -> ExecutionReservation:
        """Record that the external boundary was crossed for this execution.

        Called once the mutation has actually been dispatched. After this
        succeeds the reservation is permanently claimed: :attr:`may_have_
        crossed_boundary` is true from here on, and no transition returns it
        to ``RESERVED``.

        ``worker_id`` must be the worker that holds the reservation. See
        :class:`ReservationOwnershipError` for why that is enforced rather
        than assumed.
        """
        return self._transition(
            intent_key,
            ticket_id,
            ExecutionReservationState.ATTEMPTED,
            worker_id=worker_id,
            outcome=None,
            expected_revision=expected_revision,
        )

    def record_outcome(
        self,
        intent_key: str,
        ticket_id: str,
        outcome: ExecutionOutcome,
        *,
        worker_id: str,
        expected_revision: int | None = None,
    ) -> ExecutionReservation:
        """Record what happened, moving to ``RESOLVED`` or ``UNRESOLVED``.

        The resulting state is derived from the outcome rather than chosen
        by the caller, and deliberately cannot be mis-paired:

        * ``ExecutionOutcome.UNKNOWN`` -- the attempt or its result could
          not be firmly established -- yields ``UNRESOLVED``, which is
          terminal and never automatically retried.
        * any other outcome yields ``RESOLVED``.

        Letting the caller pass a state alongside the outcome would allow
        the one combination this module exists to prevent: an
        ``UNKNOWN`` outcome recorded as a settled resolution. Deriving the
        state here makes that unrepresentable.

        ``worker_id`` must be the worker that holds the reservation.
        """
        if not isinstance(outcome, ExecutionOutcome):
            raise TypeError(
                "record_outcome requires an ExecutionOutcome, got "
                f"{type(outcome).__name__}"
            )
        state = (
            ExecutionReservationState.UNRESOLVED
            if outcome is ExecutionOutcome.UNKNOWN
            else ExecutionReservationState.RESOLVED
        )
        return self._transition(
            intent_key,
            ticket_id,
            state,
            worker_id=worker_id,
            outcome=outcome,
            expected_revision=expected_revision,
        )

    def open_executions(self) -> tuple[ExecutionReservation, ...]:
        """Return every execution that is neither resolved nor unresolved.

        Read-only and ordered by ``(intent_key, ticket_id)`` so the result is
        deterministic. This is the read model a future reconciliation layer
        consumes; it performs no repair and changes no state.
        """
        with self._connection() as conn:
            rows = self._fetch_all(
                conn,
                "SELECT * FROM execution_reservation "
                f"WHERE state IN ({', '.join('?' * len(_OPEN_STATES))}) "
                "ORDER BY intent_key, ticket_id",
                tuple(sorted(s.value for s in _OPEN_STATES)),
            )
        return tuple(self._row_to_reservation(row) for row in rows)

    def unresolved_executions(self) -> tuple[ExecutionReservation, ...]:
        """Return every execution that may have crossed the boundary and has
        no established outcome.

        The direct analogue of M10's ``OpenTransaction``, widened to include
        terminal ``UNRESOLVED`` rows. Both belong here: an ``ATTEMPTED`` row
        is unsettled and still awaiting an outcome, and an ``UNRESOLVED`` row
        is a standing question about AWS that will never be answered by
        automation. Excluding the second would bury the rows that most need
        attention behind a method whose name promises them. Callers tell them
        apart with ``is_terminal``, and nothing here is repaired or retried.
        Read-only.
        """
        unsettled = (
            ExecutionReservationState.ATTEMPTED,
            ExecutionReservationState.UNRESOLVED,
        )
        with self._connection() as conn:
            rows = self._fetch_all(
                conn,
                "SELECT * FROM execution_reservation "
                f"WHERE state IN ({', '.join('?' * len(unsettled))}) "
                "ORDER BY intent_key, ticket_id",
                tuple(state.value for state in unsettled),
            )
        return tuple(self._row_to_reservation(row) for row in rows)

    def all_executions(self) -> tuple[ExecutionReservation, ...]:
        """Return every stored reservation, ordered deterministically."""
        with self._connection() as conn:
            rows = self._fetch_all(
                conn,
                "SELECT * FROM execution_reservation "
                "ORDER BY intent_key, ticket_id",
            )
        return tuple(self._row_to_reservation(row) for row in rows)

    def verify(self) -> None:
        """Fail closed unless the ledger is internally consistent.

        Checks, in order: SQLite's own page-level integrity, the recorded
        schema version, that every row parses as a valid reservation with a
        known state and outcome, and that the uniqueness invariant actually
        holds. A ``RESOLVED`` or ``UNRESOLVED`` row with no outcome, or a
        ``RESERVED`` row carrying one, is corruption rather than a tolerated
        oddity: both mean the ledger is describing an execution state it
        cannot justify.
        """
        with self._connection() as conn:
            try:
                page = conn.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.DatabaseError as exc:
                raise ExecutionLedgerCorruptionError(
                    f"execution ledger {self._path} failed integrity_check: {exc}"
                ) from exc
            if page is None or str(page[0]).lower() != "ok":
                raise ExecutionLedgerCorruptionError(
                    f"execution ledger {self._path} is structurally corrupt: "
                    f"{page[0] if page else 'no result'}"
                )
            self._verify_schema_version(conn)
            pairs: set[tuple[str, str]] = set()
            for row in self._fetch_all(conn, "SELECT * FROM execution_reservation"):
                try:
                    reservation = self._row_to_reservation(row)
                except ExecutionLedgerCorruptionError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reported, never repaired
                    raise ExecutionLedgerCorruptionError(
                        f"execution ledger {self._path} holds an invalid "
                        f"reservation {row['reservation_id']!r}: {exc}"
                    ) from exc
                pair = (reservation.intent_key, reservation.ticket_id)
                if pair in pairs:
                    raise ExecutionLedgerCorruptionError(
                        f"execution ledger {self._path} holds two reservations "
                        f"for intent {reservation.intent_key!r} and ticket "
                        f"{reservation.ticket_id!r}"
                    )
                pairs.add(pair)
                self._verify_state_outcome(reservation)

    def close(self) -> None:
        """Mark the ledger unusable. Idempotent.

        Connections are per-operation, so there is nothing to release; the
        flag exists so a use-after-close fails loudly instead of silently
        appearing to succeed.
        """
        self._closed = True

    @property
    def path(self) -> Path:
        """Filesystem location of the ledger."""
        return self._path

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DurableExecutionLedger(path={str(self._path)!r})"

    # -- internals ---------------------------------------------------------

    def _verify_state_outcome(
        self, reservation: ExecutionReservation
    ) -> None:
        """Reject a row whose state and outcome contradict each other."""
        state = reservation.state
        outcome = reservation.outcome
        if state is ExecutionReservationState.UNRESOLVED:
            if outcome is not ExecutionOutcome.UNKNOWN:
                raise ExecutionLedgerCorruptionError(
                    f"execution ledger {self._path} reservation "
                    f"{reservation.reservation_id!r} is UNRESOLVED but records "
                    f"outcome {outcome!r}; an unresolved execution must record "
                    "ExecutionOutcome.UNKNOWN"
                )
            return
        if state is ExecutionReservationState.RESOLVED:
            if outcome is None:
                raise ExecutionLedgerCorruptionError(
                    f"execution ledger {self._path} reservation "
                    f"{reservation.reservation_id!r} is RESOLVED but records no "
                    "outcome"
                )
            return
        if outcome is not None:
            raise ExecutionLedgerCorruptionError(
                f"execution ledger {self._path} reservation "
                f"{reservation.reservation_id!r} is {state.value} yet records "
                f"outcome {outcome!r}; only a terminal state may carry one"
            )

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self._path,
                isolation_level=None,
                timeout=self._busy_timeout,
            )
        except sqlite3.Error as exc:
            raise ExecutionLedgerUnavailableError(
                f"could not open execution ledger {self._path}: {exc}"
            ) from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                f"PRAGMA busy_timeout = {int(self._busy_timeout * 1000)}"
            )
            # Durability over throughput: an execution claim is written a
            # handful of times per execution, and a committed reservation must
            # survive a power loss, not merely a process crash.
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.OperationalError as exc:
            # Transient or environmental: locked, busy, read-only, out of
            # space. Worth retrying.
            conn.close()
            raise ExecutionLedgerUnavailableError(
                f"could not configure execution ledger {self._path}: {exc}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            # The file exists but is not a usable database -- junk bytes, a
            # truncated header, another format. Retrying cannot help, so this
            # must not masquerade as a transient condition.
            conn.close()
            raise ExecutionLedgerCorruptionError(
                f"execution ledger {self._path} is not a readable SQLite "
                f"database: {exc}"
            ) from exc
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise ExecutionLedgerUnavailableError(
                f"execution ledger {self._path} is closed"
            )
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction, rolling back on any failure.

        ``BEGIN IMMEDIATE`` takes SQLite's write lock at the start, so two
        writers serialize here instead of racing and deadlocking on upgrade.
        That serialization is what makes :meth:`reserve` exactly-one-winner:
        the existence check and the insert cannot interleave.
        """
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise ExecutionLedgerUnavailableError(
                    f"execution ledger {self._path} is busy: {exc}"
                ) from exc
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - rollback failed too
                    pass
                raise
            try:
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover
                    pass
                raise ExecutionLedgerUnavailableError(
                    f"could not commit execution ledger {self._path}: {exc}"
                ) from exc

    def _initialize(self) -> None:
        """Create the schema on first use; validate it on every later open.

        A file that already existed must already be a ledger. Silently running
        ``CREATE TABLE IF NOT EXISTS`` against a foreign or damaged file
        would let the store "repair" it into an empty-but-valid execution
        database, quietly discarding whatever was there.
        """
        if self._preexisting:
            self._adopt_existing()
            return
        with self._transaction() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(EXECUTION_LEDGER_SCHEMA_VERSION),),
                )
            elif str(row[0]) != str(EXECUTION_LEDGER_SCHEMA_VERSION):
                raise ExecutionLedgerCorruptionError(
                    f"execution ledger {self._path} has schema version "
                    f"{row[0]}, this build understands "
                    f"{EXECUTION_LEDGER_SCHEMA_VERSION}"
                )

    def _adopt_existing(self) -> None:
        """Open an existing ledger, tolerating a concurrent first open.

        Two processes can legitimately race here: one wins the right to
        create the schema while the other arrives just after the file exists
        but before that schema is committed. An empty ``sqlite_master`` is
        the only signal for that state, and it is indistinguishable from
        "this file is not a ledger", so the empty case is retried briefly
        and then fails closed.
        """
        for attempt in range(_EMPTY_SCHEMA_RETRIES):
            with self._connection() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if not tables:
                    if attempt + 1 < _EMPTY_SCHEMA_RETRIES:
                        sleep(_EMPTY_SCHEMA_RETRY_SECONDS)
                        continue
                    raise ExecutionLedgerCorruptionError(
                        f"execution ledger {self._path} has no tables; refusing "
                        "to treat a non-ledger file as an execution authority"
                    )
                self._verify_schema_version(conn)
                return

    def _verify_schema_version(self, conn: sqlite3.Connection) -> None:
        # A damaged page surfaces here as a bare sqlite3.DatabaseError. It is
        # re-raised as corruption rather than allowed to escape: callers of this
        # module are written against ExecutionLedgerError, and an untyped
        # driver error from a durability check is exactly the kind of thing
        # that gets caught and ignored at a call site.
        try:
            present = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing = [name for name in _REQUIRED_TABLES if name not in present]
            if missing:
                raise ExecutionLedgerCorruptionError(
                    f"execution ledger {self._path} has no "
                    f"{'table' if len(missing) == 1 else 'tables'} "
                    f"{', '.join(missing)}; refusing to treat a foreign file as an "
                    "execution authority"
                )
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except ExecutionLedgerCorruptionError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ExecutionLedgerCorruptionError(
                f"execution ledger {self._path} is unreadable while checking its "
                f"schema: {exc}"
            ) from exc
        if row is None:
            raise ExecutionLedgerCorruptionError(
                f"execution ledger {self._path} has no recorded schema version"
            )
        if str(row[0]) != str(EXECUTION_LEDGER_SCHEMA_VERSION):
            raise ExecutionLedgerCorruptionError(
                f"execution ledger {self._path} has schema version {row[0]}, "
                f"this build understands {EXECUTION_LEDGER_SCHEMA_VERSION}"
            )

    def _fetch_all(
        self, conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
    ) -> list[sqlite3.Row]:
        """Run a read query, keeping driver errors inside this module's types.

        A structurally damaged page raises ``sqlite3.DatabaseError`` from any
        ``execute``, not just from ``PRAGMA integrity_check``. Without this,
        the first read a caller happens to issue is the one that leaks an
        untyped error out of a durability-checked component.
        """
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ExecutionLedgerCorruptionError(
                f"execution ledger {self._path} could not be read: {exc}"
            ) from exc

    def _select(self, conn: sqlite3.Connection, reservation_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM execution_reservation WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()

    def _select_pair(
        self, conn: sqlite3.Connection, intent_key: str, ticket_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM execution_reservation "
            "WHERE intent_key = ? AND ticket_id = ?",
            (intent_key, ticket_id),
        ).fetchone()

    def _raise_conflict_for(
        self, row: sqlite3.Row, attempted_ticket_revision: int | None = None
    ) -> None:
        """Raise the refusal that matches an already-stored row's state.

        Raises rather than returns so the caller inside :meth:`reserve`
        cannot accidentally continue after a conflict.

        ``attempted_ticket_revision`` is checked first, before the state, because
        a mismatch names a different authorization instance rather than a
        second claim on the recorded one. Reporting it as merely
        ``AlreadyReservedError`` would hide the real fault: the caller asked
        about approval revision 4 and would be told, misleadingly, that
        revision 3 is busy.
        """
        if attempted_ticket_revision is not None:
            recorded = int(row["ticket_revision"])
            if recorded != attempted_ticket_revision:
                pair = f"intent {row['intent_key']!r} and ticket {row['ticket_id']!r}"
                raise TicketRevisionConflictError(
                    f"execution for {pair} is already reserved against approval "
                    f"revision {recorded}, not revision "
                    f"{attempted_ticket_revision}; the claim is bound to the "
                    "authorization instance it was created against"
                )
        state = ExecutionReservationState(row["state"])
        pair = f"intent {row['intent_key']!r} and ticket {row['ticket_id']!r}"
        if state is ExecutionReservationState.RESERVED:
            raise AlreadyReservedError(
                f"execution for {pair} is already RESERVED by worker "
                f"{row['worker_id']!r} at revision {row['revision']}; "
                "refusing a second claim"
            )
        if state is ExecutionReservationState.ATTEMPTED:
            raise AlreadyAttemptedError(
                f"execution for {pair} is already ATTEMPTED by worker "
                f"{row['worker_id']!r}; the external boundary is recorded as "
                "crossed, so no other worker may execute it"
            )
        if state is ExecutionReservationState.UNRESOLVED:
            raise AlreadyResolvedError(
                f"execution for {pair} is UNRESOLVED (outcome "
                f"{row['outcome']!r}); it is terminal and is never retried "
                "automatically, so a new approval is required"
            )
        raise AlreadyResolvedError(
            f"execution for {pair} is already RESOLVED with outcome "
            f"{row['outcome']!r}; it is terminal"
        )

    def _raise_conflict_for_uncommitted(
        self,
        conn: sqlite3.Connection,
        intent_key: str,
        ticket_id: str,
        ticket_revision: int,
        exc: sqlite3.IntegrityError,
    ) -> None:
        """Report a uniqueness violation in this module's vocabulary.

        Reached only if the ``UNIQUE (intent_key, ticket_id)`` constraint
        fired, which ``BEGIN IMMEDIATE`` should have made unreachable by
        serializing the check and the insert. It is kept because the
        constraint is the real guarantee and its violation must surface as a
        typed refusal rather than as a raw ``sqlite3`` error a caller cannot
        meaningfully handle.
        """
        row = self._select_pair(conn, intent_key, ticket_id)
        if row is not None:
            self._raise_conflict_for(row, ticket_revision)
        raise ExecutionLedgerUnavailableError(  # pragma: no cover - defensive
            f"execution ledger {self._path} rejected a reservation insert: {exc}"
        )

    def _transition(
        self,
        intent_key: str,
        ticket_id: str,
        new_state: ExecutionReservationState,
        *,
        worker_id: str,
        outcome: ExecutionOutcome | None,
        expected_revision: int | None,
    ) -> ExecutionReservation:
        """Apply one state transition atomically, or not at all.

        Three ordered gates refuse before anything is written:

        1. the transition table, so this store accepts exactly the moves
           declared in :data:`LEGAL_EXECUTION_TRANSITIONS`;
        2. ownership, so only the worker holding the reservation may move it;
        3. the caller's ``expected_revision`` precondition.

        The compare-and-swap then runs inside the same transaction. Its
        ``expected`` value is the revision observed before the write, so the
        CAS is what catches a writer that won the race between that read and
        this write. There is no automatic retry: a conflict means the caller
        must re-read and decide, not that the store should try again.
        """
        if not worker_id:
            raise ValueError("worker_id is required to transition an execution")
        with self._connection() as conn:
            row = self._select_pair(conn, intent_key, ticket_id)
        if row is None:
            raise UnknownReservationError(
                f"no execution reservation for intent {intent_key!r} and "
                f"ticket {ticket_id!r}"
            )

        owner = str(row["worker_id"])
        if owner != worker_id:
            raise ReservationOwnershipError(
                f"execution reservation {row['reservation_id']!r} for intent "
                f"{intent_key!r} and ticket {ticket_id!r} is held by worker "
                f"{owner!r}, not {worker_id!r}; a worker may only transition "
                "the execution it claimed"
            )

        current = ExecutionReservationState(row["state"])
        permitted = LEGAL_EXECUTION_TRANSITIONS[current]
        if new_state not in permitted:
            allowed = ", ".join(sorted(s.value for s in permitted)) or "none"
            raise InvalidExecutionTransitionError(
                f"execution {row['reservation_id']!r} cannot transition from "
                f"{current.value} to {new_state.value} (legal targets from "
                f"{current.value}: {allowed})"
            )

        current_revision = int(row["revision"])
        expected = current_revision if expected_revision is None else expected_revision
        if expected != current_revision:
            raise ReservationRevisionConflictError(
                f"execution {row['reservation_id']!r} is at revision "
                f"{current_revision}, but revision {expected} was expected; "
                "refusing to overwrite newer execution state"
            )

        moment = self._now()
        with self._transaction() as conn:
            self._commit_update(
                conn,
                reservation_id=str(row["reservation_id"]),
                new_state=new_state,
                outcome=outcome,
                expected=expected,
                moment=moment,
            )
        with self._connection() as conn:
            refreshed = self._select(conn, str(row["reservation_id"]))
        assert refreshed is not None  # noqa: S101 - row exists
        return self._row_to_reservation(refreshed)

    def _commit_update(
        self,
        conn: sqlite3.Connection,
        *,
        reservation_id: str,
        new_state: ExecutionReservationState,
        outcome: ExecutionOutcome | None,
        expected: int,
        moment: datetime,
    ) -> None:
        """Compare-and-swap the row, requiring exactly one changed row.

        Only fields a transition can change are written: ``intent_key``,
        ``ticket_id``, ``ticket_revision``, ``worker_id``, and the identity
        fields are fixed at reservation time, so leaving them out of the SET
        list makes their immutability here obvious rather than merely true.

        The WHERE clause is the precondition. If another writer advanced the
        reservation past ``expected`` first, no row matches and the change
        is refused -- the store never overwrites newer execution state.
        """
        cursor = conn.execute(
            "UPDATE execution_reservation SET "
            "state = ?, outcome = ?, revision = ?, updated_at = ? "
            "WHERE reservation_id = ? AND revision = ?",
            (
                new_state.value,
                outcome.value if outcome is not None else None,
                expected + 1,
                _format_ts(moment),
                reservation_id,
                expected,
            ),
        )
        if cursor.rowcount != 1:
            raise ReservationRevisionConflictError(
                f"execution {reservation_id!r} changed underneath this "
                f"transition: expected revision {expected} matched no row"
            )

    def _row_to_reservation(self, row: sqlite3.Row) -> ExecutionReservation:
        """Rehydrate a stored row, failing closed on anything unexpected."""
        state_raw = row["state"]
        try:
            state = ExecutionReservationState(state_raw)
        except ValueError as exc:
            raise ExecutionLedgerCorruptionError(
                f"execution ledger {self._path} holds unknown state "
                f"{state_raw!r} for reservation {row['reservation_id']!r}"
            ) from exc
        action_raw = row["action"]
        action: PotentialAction | None = None
        if action_raw is not None:
            try:
                action = PotentialAction(action_raw)
            except ValueError as exc:
                raise ExecutionLedgerCorruptionError(
                    f"execution ledger {self._path} holds unknown action "
                    f"{action_raw!r} for reservation {row['reservation_id']!r}"
                ) from exc
        outcome_raw = row["outcome"]
        outcome: ExecutionOutcome | None = None
        if outcome_raw is not None:
            try:
                outcome = ExecutionOutcome(outcome_raw)
            except ValueError as exc:
                raise ExecutionLedgerCorruptionError(
                    f"execution ledger {self._path} holds unknown outcome "
                    f"{outcome_raw!r} for reservation {row['reservation_id']!r}"
                ) from exc
        return ExecutionReservation(
            reservation_id=str(row["reservation_id"]),
            intent_key=str(row["intent_key"]),
            ticket_id=str(row["ticket_id"]),
            ticket_revision=int(row["ticket_revision"]),
            state=state,
            revision=int(row["revision"]),
            worker_id=str(row["worker_id"]),
            created_at=_parse_ts(row["created_at"]),
            updated_at=_parse_ts(row["updated_at"]),
            action_plan_id=row["action_plan_id"],
            resource_id=row["resource_id"],
            action=action,
            outcome=outcome,
            stale_after=self._stale_after,
        )


def _format_ts(value: datetime | None) -> str | None:
    """Render an aware datetime as canonical UTC ISO-8601.

    Normalizing to UTC makes the stored text a single canonical form. A naive
    datetime is rejected rather than assumed to be UTC: guessing the offset
    is how an execution claim quietly appears to be hours old or new.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("refusing to persist a naive datetime")
    return value.astimezone(timezone.utc).isoformat()


def _parse_ts(value: str | None) -> datetime:
    """Parse a stored timestamp, failing closed on naive or malformed text."""
    if value is None:
        raise ExecutionLedgerCorruptionError(
            "execution ledger holds a missing required timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExecutionLedgerCorruptionError(
            f"execution ledger holds an unparseable timestamp {value!r}: {exc}"
        ) from exc
    if parsed.tzinfo is None:
        raise ExecutionLedgerCorruptionError(
            f"execution ledger holds a naive timestamp {value!r}"
        )
    return parsed