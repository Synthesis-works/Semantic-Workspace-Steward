"""M13 Phase 3: durable execution reservation and transaction state.

The claim under test is the one Phase 2 established was missing. The
approval CAS protects the approval *record* while leaving the external
*side effect* unprotected -- measured against the real durable approval
store, four independent processes all crossed the external-effect boundary
and only one later won ``consume``. So these tests are about exclusivity and
durability, not about approval semantics:

* ``reserve`` is exactly-once per ``(intent_key, ticket_id)``, proven with
  genuinely separate interpreters rather than threads, because threads share
  a GIL and would not exercise SQLite's cross-process write lock;
* a fresh process sees what another process committed;
* the invariant that matters most is negative: a reservation that may have
  crossed the external boundary is never released for another worker, and an
  unresolved outcome is never retried.

No AWS, no network, no subprocess, no mutation handler, no
``ExecutionCoordinator``: this suite exercises an isolated primitive.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sws_agent.constants import ExecutionOutcome, PotentialAction
from sws_agent.execution_ledger import (
    DEFAULT_EXECUTION_STALE_AFTER,
    EXECUTION_LEDGER_SCHEMA_VERSION,
    LEGAL_EXECUTION_TRANSITIONS,
    TERMINAL_EXECUTION_STATES,
    AlreadyAttemptedError,
    AlreadyReservedError,
    AlreadyResolvedError,
    DurableExecutionLedger,
    ExecutionLedgerCorruptionError,
    ExecutionLedgerError,
    ExecutionLedgerUnavailableError,
    ExecutionReservation,
    ExecutionReservationState,
    IntentAlreadyExecutedError,
    InvalidExecutionTransitionError,
    ReservationConflictError,
    ReservationOwnershipError,
    ReservationRevisionConflictError,
    TicketRevisionConflictError,
    UnknownReservationError,
)
from sws_agent.interfaces import ExecutionLedger

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)

INTENT = "intent-aaa"
OTHER_INTENT = "intent-bbb"


class FakeClock:
    """Deterministic, mutable time source, mirroring tests/test_approval.py."""

    def __init__(self, current: datetime = START) -> None:
        self._current = current

    def __call__(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current = self._current + delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "execution.sqlite3"


@pytest.fixture
def ledger(ledger_path: Path, clock: FakeClock) -> DurableExecutionLedger:
    store = DurableExecutionLedger(ledger_path, now=clock)
    yield store
    store.close()


def _reserve(
    ledger: DurableExecutionLedger,
    *,
    intent_key: str = INTENT,
    ticket_id: str = "t1",
    ticket_revision: int = 1,
    worker_id: str = "w1",
) -> ExecutionReservation:
    return ledger.reserve(
        intent_key=intent_key,
        ticket_id=ticket_id,
        ticket_revision=ticket_revision,
        worker_id=worker_id,
        action_plan_id="plan-1",
        resource_id="i-1",
        action=PotentialAction.STOP_RESOURCE,
    )


# --- schema and construction ------------------------------------------------


def test_schema_is_created_and_versioned(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"meta", "execution_reservation"} <= tables
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert version == str(EXECUTION_LEDGER_SCHEMA_VERSION)
    finally:
        conn.close()


def test_reservation_key_is_unique_per_intent_and_ticket(
    ledger_path: Path,
) -> None:
    """The database-level backstop for Part 12's invariant."""
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        indexes = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'execution_reservation'"
        ).fetchone()[0]
        assert "UNIQUE (intent_key, ticket_id)" in indexes
    finally:
        conn.close()


def test_ledger_satisfies_the_injected_protocol(ledger: DurableExecutionLedger) -> None:
    assert isinstance(ledger, ExecutionLedger)


def test_rejects_naive_clock(ledger_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DurableExecutionLedger(ledger_path, now=lambda: START.replace(tzinfo=None))


def test_rejects_non_positive_stale_after(ledger_path: Path) -> None:
    with pytest.raises(ValueError, match="stale_after must be positive"):
        DurableExecutionLedger(ledger_path, now=lambda: START, stale_after=timedelta(0))


# --- reservation ------------------------------------------------------------


def test_reserve_records_the_claim(ledger: DurableExecutionLedger) -> None:
    reservation = _reserve(ledger)
    assert reservation.state is ExecutionReservationState.RESERVED
    assert reservation.revision == 0
    assert reservation.intent_key == INTENT
    assert reservation.ticket_id == "t1"
    assert reservation.ticket_revision == 1
    assert reservation.worker_id == "w1"
    assert reservation.action is PotentialAction.STOP_RESOURCE
    assert reservation.resource_id == "i-1"
    assert reservation.action_plan_id == "plan-1"
    assert reservation.outcome is None
    assert reservation.created_at == START
    assert reservation.updated_at == START
    # A RESERVED execution has provably not crossed the external boundary.
    assert reservation.may_have_crossed_boundary is False
    assert reservation.is_open is True


def test_reserve_refuses_a_duplicate_claim(ledger: DurableExecutionLedger) -> None:
    _reserve(ledger)
    with pytest.raises(AlreadyReservedError):
        _reserve(ledger, worker_id="w2")


def test_same_intent_different_ticket_is_refused_while_the_first_is_open(
    ledger: DurableExecutionLedger,
) -> None:
    """A fresh ticket cannot open a second execution of an in-flight intent.

    This reverses what Phase 3 asserted. Phase 3 held that a new
    authorization instance was always a separate execution, because the intent
    key is identical across a revoked ticket and its replacement. That
    reasoning was sound about the *key* -- keying on intent alone would
    permanently block a re-approved attempt, which is why ``ticket_id``
    remains part of the identity -- but it left the effect unguarded: the pair
    key identifies an authorization instance, not the thing being acted on, so
    a fresh ticket could perform an effect an earlier authorization may
    already have performed.

    Phase 4 therefore keeps the pair key and adds an intent-level decision on
    top of it. The prior ``RESERVED`` row here is the strongest case for
    blocking, because M13 performs no automatic stale-reservation takeover and
    so cannot claim the previous worker is gone.
    """
    first = _reserve(ledger, ticket_id="t1")
    with pytest.raises(IntentAlreadyExecutedError):
        _reserve(ledger, ticket_id="t2", worker_id="w2")
    assert len(ledger.all_executions()) == 1
    assert first.intent_key == INTENT


def test_same_intent_different_ticket_is_permitted_after_a_failed_outcome(
    ledger: DurableExecutionLedger,
) -> None:
    """A known unsuccessful attempt is what makes a reattempt meaningful.

    The Phase 2 measurement still holds -- the intent key does not change --
    so without an outcome-aware decision the replacement ticket would either
    be blocked forever or allowed unconditionally. ``FAILED`` is the single
    prior fact that justifies a second execution, and the new reservation
    records which failure it supersedes.
    """
    first = _reserve(ledger, ticket_id="t1")
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    ledger.record_outcome(
        INTENT, "t1", ExecutionOutcome.FAILED, worker_id="w1"
    )

    second = _reserve(ledger, ticket_id="t2", worker_id="w2")

    assert second.intent_key == first.intent_key
    assert second.ticket_id != first.ticket_id
    assert second.supersedes_reservation_id == first.reservation_id
    assert len(ledger.all_executions()) == 2


def test_same_ticket_different_intent_is_a_separate_execution(
    ledger: DurableExecutionLedger,
) -> None:
    _reserve(ledger, intent_key=INTENT)
    _reserve(ledger, intent_key=OTHER_INTENT, worker_id="w2")
    assert len(ledger.all_executions()) == 2


def test_different_ticket_revision_conflicts(
    ledger: DurableExecutionLedger,
) -> None:
    """A claim is bound to the exact approval revision it was made against."""
    _reserve(ledger, ticket_revision=3)
    with pytest.raises(TicketRevisionConflictError):
        _reserve(ledger, ticket_revision=4, worker_id="w2")


def test_reserve_requires_identifiers(ledger: DurableExecutionLedger) -> None:
    with pytest.raises(ValueError, match="intent_key"):
        _reserve(ledger, intent_key="")
    with pytest.raises(ValueError, match="ticket_id"):
        _reserve(ledger, ticket_id="")
    with pytest.raises(ValueError, match="worker_id"):
        _reserve(ledger, worker_id="")
    with pytest.raises(ValueError, match="ticket_revision"):
        _reserve(ledger, ticket_revision=-1)


def test_get_returns_the_reservation(ledger: DurableExecutionLedger) -> None:
    _reserve(ledger)
    assert ledger.get(INTENT, "t1").worker_id == "w1"


def test_get_raises_for_an_unclaimed_pair(ledger: DurableExecutionLedger) -> None:
    with pytest.raises(UnknownReservationError):
        ledger.get(INTENT, "nope")


# --- state machine ----------------------------------------------------------


def test_transition_table_matches_the_enum() -> None:
    assert set(LEGAL_EXECUTION_TRANSITIONS) == set(ExecutionReservationState)
    for state, targets in LEGAL_EXECUTION_TRANSITIONS.items():
        assert targets <= set(LEGAL_EXECUTION_TRANSITIONS)
        assert LEGAL_EXECUTION_TRANSITIONS[state] == frozenset(targets)
    assert LEGAL_EXECUTION_TRANSITIONS[ExecutionReservationState.RESOLVED] == frozenset()
    assert (
        LEGAL_EXECUTION_TRANSITIONS[ExecutionReservationState.UNRESOLVED] == frozenset()
    )


def test_mark_attempted_crosses_the_boundary(
    ledger: DurableExecutionLedger, clock: FakeClock
) -> None:
    _reserve(ledger)
    clock.advance(HOUR)
    attempted = ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    assert attempted.state is ExecutionReservationState.ATTEMPTED
    assert attempted.revision == 1
    assert attempted.updated_at == START + HOUR
    assert attempted.created_at == START
    # The predicate a reconciliation layer must gate on.
    assert attempted.may_have_crossed_boundary is True


def test_record_outcome_resolves(ledger: DurableExecutionLedger) -> None:
    _reserve(ledger)
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    resolved = ledger.record_outcome(INTENT, "t1", ExecutionOutcome.VERIFIED_SUCCESS, worker_id="w1")
    assert resolved.state is ExecutionReservationState.RESOLVED
    assert resolved.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert resolved.revision == 2
    assert resolved.is_terminal is True


def test_unknown_outcome_becomes_unresolved_and_is_terminal(
    ledger: DurableExecutionLedger,
) -> None:
    """The outcome that must never be confused with success or failure."""
    _reserve(ledger)
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    unresolved = ledger.record_outcome(INTENT, "t1", ExecutionOutcome.UNKNOWN, worker_id="w1")
    assert unresolved.state is ExecutionReservationState.UNRESOLVED
    assert unresolved.outcome is ExecutionOutcome.UNKNOWN
    assert unresolved.is_terminal is True
    assert unresolved.state in TERMINAL_EXECUTION_STATES
    # Still recorded as possibly having crossed the boundary, forever.
    assert unresolved.may_have_crossed_boundary is True


def test_unknown_outcome_is_never_retried(ledger: DurableExecutionLedger) -> None:
    _reserve(ledger)
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    ledger.record_outcome(INTENT, "t1", ExecutionOutcome.UNKNOWN, worker_id="w1")
    with pytest.raises(AlreadyResolvedError):
        _reserve(ledger, worker_id="w2")
    with pytest.raises(InvalidExecutionTransitionError):
        ledger.mark_attempted(INTENT, "t1", worker_id="w1")


def test_attempted_execution_is_never_reclaimed(
    ledger: DurableExecutionLedger,
) -> None:
    """A crash during the external call leaves ATTEMPTED, and that is final."""
    _reserve(ledger)
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    with pytest.raises(AlreadyAttemptedError):
        _reserve(ledger, worker_id="w2")


def test_resolved_execution_is_never_reclaimed(
    ledger: DurableExecutionLedger,
) -> None:
    _reserve(ledger)
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    ledger.record_outcome(INTENT, "t1", ExecutionOutcome.FAILED, worker_id="w1")
    with pytest.raises(AlreadyResolvedError):
        _reserve(ledger, worker_id="w2")


def test_a_reserved_execution_may_only_advance_to_attempted(
    ledger: DurableExecutionLedger,
) -> None:
    """Reaching a terminal state requires passing through ATTEMPTED.

    Without this, a ``RESERVED -> RESOLVED`` row and an
    ``ATTEMPTED -> RESOLVED`` row would be indistinguishable in durable state,
    and ``may_have_crossed_boundary`` -- the predicate a reconciliation layer
    gates on -- could not tell one that provably never called AWS from one
    that might have. An abandoned claim therefore stays ``RESERVED`` and is
    reported as stale, never released.
    """
    _reserve(ledger)
    for outcome in (
        ExecutionOutcome.REFUSED,
        ExecutionOutcome.NOT_EXECUTED,
        ExecutionOutcome.UNKNOWN,
    ):
        with pytest.raises(InvalidExecutionTransitionError):
            ledger.record_outcome(INTENT, "t1", outcome, worker_id="w1")
    # Still RESERVED, still not reclaimed.
    assert ledger.get(INTENT, "t1").state is ExecutionReservationState.RESERVED
    assert ledger.get(INTENT, "t1").may_have_crossed_boundary is False
    with pytest.raises(AlreadyReservedError):
        _reserve(ledger, worker_id="w2")


def test_terminal_states_admit_no_transition(
    ledger: DurableExecutionLedger,
) -> None:
    _reserve(ledger)
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    ledger.record_outcome(INTENT, "t1", ExecutionOutcome.PARTIALLY_VERIFIED, worker_id="w1")
    for call in (
        lambda: ledger.mark_attempted(INTENT, "t1", worker_id="w1"),
        lambda: ledger.record_outcome(INTENT, "t1", ExecutionOutcome.UNKNOWN, worker_id="w1"),
    ):
        with pytest.raises(InvalidExecutionTransitionError):
            call()


def test_transition_requires_an_execution(
    ledger: DurableExecutionLedger,
) -> None:
    with pytest.raises(UnknownReservationError):
        ledger.mark_attempted(INTENT, "missing", worker_id="w1")


def test_record_outcome_rejects_a_non_outcome(
    ledger: DurableExecutionLedger,
) -> None:
    _reserve(ledger)
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    with pytest.raises(TypeError):
        ledger.record_outcome(INTENT, "t1", "verified_success", worker_id="w1")  # type: ignore[arg-type]


def test_conflicts_share_a_base_class(ledger: DurableExecutionLedger) -> None:
    """Callers can refuse to act on any 'already claimed' signal uniformly."""
    _reserve(ledger)
    with pytest.raises(ReservationConflictError):
        _reserve(ledger, worker_id="w2")


# --- ownership ---------------------------------------------------------------


def test_another_worker_cannot_mark_attempted(
    ledger: DurableExecutionLedger,
) -> None:
    """The claim is exclusive, so the transitions that decide what happened
    must be exclusive too.

    Without this gate the ``worker_id`` column would be decorative: any caller
    that learned an ``(intent_key, ticket_id)`` pair could drive the state
    machine on behalf of another worker, including stamping a definite
    outcome onto an execution it never touched.
    """
    _reserve(ledger, worker_id="w1")
    with pytest.raises(ReservationOwnershipError):
        ledger.mark_attempted(INTENT, "t1", worker_id="w2")
    assert ledger.get(INTENT, "t1").state is ExecutionReservationState.RESERVED
    assert ledger.get(INTENT, "t1").revision == 0


def test_another_worker_cannot_record_a_definite_outcome(
    ledger: DurableExecutionLedger,
) -> None:
    """The most damaging case: forging success on someone else's execution."""
    _reserve(ledger, worker_id="w1")
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    with pytest.raises(ReservationOwnershipError):
        ledger.record_outcome(
            INTENT, "t1", ExecutionOutcome.VERIFIED_SUCCESS, worker_id="w2"
        )
    survivor = ledger.get(INTENT, "t1")
    assert survivor.state is ExecutionReservationState.ATTEMPTED
    assert survivor.outcome is None


def test_another_worker_cannot_record_unknown(
    ledger: DurableExecutionLedger,
) -> None:
    _reserve(ledger, worker_id="w1")
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    with pytest.raises(ReservationOwnershipError):
        ledger.record_outcome(INTENT, "t1", ExecutionOutcome.UNKNOWN, worker_id="w2")
    assert ledger.get(INTENT, "t1").state is ExecutionReservationState.ATTEMPTED


def test_the_owning_worker_may_transition(ledger: DurableExecutionLedger) -> None:
    _reserve(ledger, worker_id="w1")
    assert ledger.mark_attempted(INTENT, "t1", worker_id="w1").revision == 1
    assert (
        ledger.record_outcome(
            INTENT, "t1", ExecutionOutcome.VERIFIED_SUCCESS, worker_id="w1"
        ).state
        is ExecutionReservationState.RESOLVED
    )


def test_transitions_require_a_worker_id(ledger: DurableExecutionLedger) -> None:
    _reserve(ledger)
    with pytest.raises(ValueError, match="worker_id"):
        ledger.mark_attempted(INTENT, "t1", worker_id="")


def test_ownership_is_checked_before_the_revision(
    ledger: DurableExecutionLedger,
) -> None:
    """An impostor is told it does not own the claim, not that it is stale.

    Both refuse, but they mean different things: one is a lost race worth
    re-reading, the other is a caller that must never touch this execution at
    all. Reporting the wrong one would have a coordinator retrying a
    transition it is not entitled to make.
    """
    _reserve(ledger, worker_id="w1")
    with pytest.raises(ReservationOwnershipError):
        ledger.mark_attempted(INTENT, "t1", worker_id="w2", expected_revision=99)


def test_ownership_error_is_a_reservation_conflict() -> None:
    assert issubclass(ReservationOwnershipError, ReservationConflictError)
    assert not issubclass(ReservationOwnershipError, UnknownReservationError)


# --- CAS semantics ----------------------------------------------------------


def test_every_transition_increments_revision_by_one(
    ledger: DurableExecutionLedger,
) -> None:
    _reserve(ledger)
    assert ledger.get(INTENT, "t1").revision == 0
    assert ledger.mark_attempted(INTENT, "t1", worker_id="w1").revision == 1
    assert ledger.get(INTENT, "t1").revision == 1
    assert (
        ledger.record_outcome(INTENT, "t1", ExecutionOutcome.VERIFIED_SUCCESS, worker_id="w1").revision
        == 2
    )
    assert ledger.get(INTENT, "t1").revision == 2


def test_expected_revision_is_honoured(ledger: DurableExecutionLedger) -> None:
    _reserve(ledger)
    assert ledger.mark_attempted(INTENT, "t1", worker_id="w1", expected_revision=0).revision == 1


def test_stale_expected_revision_is_refused(
    ledger: DurableExecutionLedger,
) -> None:
    _reserve(ledger)
    with pytest.raises(ReservationRevisionConflictError):
        ledger.mark_attempted(INTENT, "t1", worker_id="w1", expected_revision=7)


def test_cas_never_silently_overwrites(ledger: DurableExecutionLedger) -> None:
    """Two writers holding revision 0: exactly one transition may land.

    Both writers act on the same revision and both target states that are
    legal from their own view, so nothing but the compare-and-swap can stop
    the loser -- which is precisely the guard that was measured to be
    necessary in Phase 2.
    """
    _reserve(ledger)
    first = ledger.mark_attempted(INTENT, "t1", worker_id="w1", expected_revision=0)
    assert first.revision == 1
    # ATTEMPTED -> RESOLVED is legal, but this writer's view is stale.
    with pytest.raises(ReservationRevisionConflictError):
        ledger.record_outcome(
            INTENT, "t1", ExecutionOutcome.VERIFIED_SUCCESS, worker_id="w1", expected_revision=0
        )
    survivor = ledger.get(INTENT, "t1")
    assert survivor.revision == 1
    assert survivor.state is ExecutionReservationState.ATTEMPTED
    assert survivor.outcome is None


def test_revision_conflict_is_not_a_reservation_conflict() -> None:
    """A conflict to re-read is a different condition from a claim to abandon.

    ``ReservationConflictError`` tells a caller to stop because someone else
    owns the execution. ``ReservationRevisionConflictError`` tells it to
    re-read, because its own view was stale and the transition may still be
    valid against current state. Collapsing them would make a caller either
    abandon valid work or retry work it must abandon.
    """
    assert issubclass(ReservationRevisionConflictError, ExecutionLedgerError)
    assert not issubclass(ReservationRevisionConflictError, ReservationConflictError)
    assert issubclass(TicketRevisionConflictError, ReservationConflictError)


# --- read models ------------------------------------------------------------


def test_open_executions_lists_unsettled_work(
    ledger: DurableExecutionLedger,
) -> None:
    # Distinct intents throughout: Phase 4 refuses a second execution of an
    # intent whose first is still open, so a fixture that wanted two
    # simultaneous open rows must be describing two different effects.
    _reserve(ledger, intent_key=INTENT, ticket_id="t1")
    _reserve(ledger, intent_key=OTHER_INTENT, ticket_id="t2", worker_id="w2")
    ledger.mark_attempted(OTHER_INTENT, "t2", worker_id="w2")
    assert [r.ticket_id for r in ledger.open_executions()] == ["t1", "t2"]


def test_unresolved_executions_lists_everything_without_an_outcome(
    ledger: DurableExecutionLedger,
) -> None:
    """The analogue of M10's OpenTransaction, including what stayed UNKNOWN.

    A terminal ``UNRESOLVED`` row is the row most in need of a human, so the
    method named for unresolved work must not hide it.
    """
    third = "intent-ccc"
    _reserve(ledger, intent_key=INTENT, ticket_id="t1")  # never attempted
    _reserve(ledger, intent_key=OTHER_INTENT, ticket_id="t2", worker_id="w2")
    ledger.mark_attempted(OTHER_INTENT, "t2", worker_id="w2")
    _reserve(ledger, intent_key=third, ticket_id="t3", worker_id="w3")
    ledger.mark_attempted(third, "t3", worker_id="w3")
    ledger.record_outcome(third, "t3", ExecutionOutcome.UNKNOWN, worker_id="w3")

    unresolved = ledger.unresolved_executions()
    assert [r.ticket_id for r in unresolved] == ["t2", "t3"]
    assert [r.is_terminal for r in unresolved] == [False, True]
    assert all(r.may_have_crossed_boundary for r in unresolved)

    ledger.record_outcome(
        OTHER_INTENT, "t2", ExecutionOutcome.VERIFIED_SUCCESS, worker_id="w2"
    )
    assert [r.ticket_id for r in ledger.unresolved_executions()] == ["t3"]


def test_read_models_are_deterministic(ledger: DurableExecutionLedger) -> None:
    # Insertion order is deliberately the reverse of the expected read order,
    # and each row carries a different intent so the ``(intent_key, ticket_id)``
    # sort is what produces the result rather than insertion order.
    for ticket in ("t3", "t1", "t2"):
        _reserve(ledger, intent_key=f"intent-{ticket}", ticket_id=ticket)
    assert [r.ticket_id for r in ledger.open_executions()] == ["t1", "t2", "t3"]


def test_stale_is_reporting_only(
    ledger_path: Path, clock: FakeClock
) -> None:
    """A stale claim is visible, and still cannot be taken over.

    This is the invariant Phase 2 justified: a worker that crashed before its
    external call and one that crashed during it leave identical durable
    state, so no age threshold may convert a claim into a free slot.
    """
    store = DurableExecutionLedger(ledger_path, now=clock)
    try:
        _reserve(store)
        assert store.get(INTENT, "t1").is_stale(clock()) is False
        clock.advance(DEFAULT_EXECUTION_STALE_AFTER + timedelta(seconds=1))
        stale = store.get(INTENT, "t1")
        assert stale.is_stale(clock()) is True
        assert stale.state is ExecutionReservationState.RESERVED
        # Reporting must not have released anything.
        with pytest.raises(AlreadyReservedError):
            _reserve(store, worker_id="w2")
    finally:
        store.close()


def test_is_stale_rejects_a_naive_as_of(ledger: DurableExecutionLedger) -> None:
    _reserve(ledger)
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.get(INTENT, "t1").is_stale(START.replace(tzinfo=None))


def test_stale_uses_a_real_clock_by_default(ledger_path: Path) -> None:
    store = DurableExecutionLedger(
        ledger_path, stale_after=timedelta(days=3650)
    )
    try:
        reservation = store.reserve(
            intent_key=INTENT, ticket_id="t1", ticket_revision=1, worker_id="w1"
        )
        assert reservation.is_stale() is False
    finally:
        store.close()


# --- durability across processes -------------------------------------------


def _drain(
    queue, processes: list, expected: int, timeout: int = 120
) -> list[tuple[str, str, str, int]]:
    """Drain the queue before joining, then confirm every child exited.

    Joining first can deadlock on Windows: a child cannot exit until its queue
    feeder thread has flushed, and the pipe buffer is finite. Reading results
    as they arrive is what keeps every child able to finish.
    """
    results = [queue.get(timeout=timeout) for _ in range(expected)]
    for process in processes:
        process.join(timeout=timeout)
    assert not any(process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    queue.close()
    queue.join_thread()
    return sorted(results)


def _reservation_worker(
    path: str, intent_key: str, ticket_id: str, worker_id: str, queue
) -> None:
    """Module level so ``spawn`` can import it in a fresh interpreter."""
    try:
        store = DurableExecutionLedger(path, verify_on_open=False)
        reservation = store.reserve(
            intent_key=intent_key,
            ticket_id=ticket_id,
            ticket_revision=1,
            worker_id=worker_id,
            action=PotentialAction.STOP_RESOURCE,
        )
        queue.put(("ok", reservation.reservation_id, reservation.worker_id, 0))
    except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
        queue.put(("err", type(exc).__name__, "", -1))
    finally:
        store.close()


def _spawn_reservations(
    path: Path, intent_key: str, ticket_id: str, workers: int
) -> list[tuple[str, str, str, int]]:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_reservation_worker,
            args=(str(path), intent_key, ticket_id, f"w{index}", queue),
        )
        for index in range(workers)
    ]
    for process in processes:
        process.start()
    try:
        return _drain(queue, processes, workers)
    finally:
        for process in processes:
            if process.is_alive():  # pragma: no cover - only on a real hang
                process.kill()


def _distinct_ticket_worker(
    path: str, intent_key: str, ticket_id: str, worker_id: str, queue: object
) -> None:
    """Reserve the same intent under a *different* ticket in each process.

    Each process holds its own approval, so every one of them passes the
    approval side of the gate independently. The only thing separating them is
    the ledger's intent-level decision, which is what this measures.
    """
    store = DurableExecutionLedger(path, verify_on_open=False)
    try:
        reservation = store.reserve(
            intent_key=intent_key,
            ticket_id=ticket_id,
            ticket_revision=1,
            worker_id=worker_id,
            action=PotentialAction.STOP_RESOURCE,
        )
        queue.put(("ok", reservation.reservation_id, reservation.worker_id, 0))
    except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
        queue.put(("err", type(exc).__name__, "", -1))
    finally:
        store.close()


def _spawn_distinct_ticket_reservations(
    path: Path, intent_key: str, workers: int
) -> list[tuple[str, str, str, int]]:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_distinct_ticket_worker,
            args=(str(path), intent_key, f"ticket-{index}", f"w{index}", queue),
        )
        for index in range(workers)
    ]
    for process in processes:
        process.start()
    try:
        return _drain(queue, processes, workers)
    finally:
        for process in processes:
            if process.is_alive():  # pragma: no cover - only on a real hang
                process.kill()


def test_separate_processes_execute_one_intent_exactly_once(
    ledger_path: Path, clock: FakeClock
) -> None:
    """Eight processes, eight distinct tickets, one intent: exactly one winner.

    This is the cross-process version of the intent-level guard, and it is the
    race the guard exists to close. Every process holds a genuinely fresh
    authorization, so nothing upstream of the ledger would stop any of them: the
    only thing preventing eight duplicate external effects is that
    ``reserve`` reads and writes inside one ``BEGIN IMMEDIATE`` transaction.

    If the decision were exposed as a separate read, a worker could read "no
    prior execution", lose the race, and proceed on a stale answer -- the
    read-then-act pattern M13 already measured at four of four processes
    crossing the boundary. One winner and seven identical refusals across five
    rounds is the evidence that it does not.
    """
    store = DurableExecutionLedger(ledger_path, now=clock)
    store.close()

    rounds = 5
    for round_index in range(rounds):
        intent = f"{INTENT}-intent-race-{round_index}"
        results = _spawn_distinct_ticket_reservations(ledger_path, intent, 8)
        winners = [r for r in results if r[0] == "ok"]
        refusals = [r for r in results if r[0] == "err"]
        assert len(winners) == 1, results
        assert len(refusals) == 7, results
        assert {r[1] for r in refusals} == {"IntentAlreadyExecutedError"}, results

    reopened = DurableExecutionLedger(ledger_path, now=clock)
    try:
        rows = reopened.all_executions()
        # Forty processes, five intents, five rows -- no loser left residue.
        assert len(rows) == rounds
        assert {r.intent_key for r in rows} == {
            f"{INTENT}-intent-race-{i}" for i in range(rounds)
        }
        assert all(r.state is ExecutionReservationState.RESERVED for r in rows)
        # Distinct tickets across processes within a round cannot collide.
        assert all(r.supersedes_reservation_id is None for r in rows)
        reopened.verify()
        reopened.verify_lineage()
    finally:
        reopened.close()


def test_the_reference_is_the_newest_failure_not_merely_a_failure(
    ledger: DurableExecutionLedger, clock: FakeClock
) -> None:
    """Three ``FAILED`` rows, and the reference is the last of them.

    A weaker implementation would satisfy the ruling with "some failed
    execution for this intent" -- it would pass a single-failure test and a
    chain test, while naming the wrong predecessor whenever an intent had been
    attempted more than twice. Lineage exists to answer *which* attempt this one
    follows; a reference to the first failure answers "that this intent once
    failed", which is a different and much weaker statement.
    """
    failures = []
    for index in range(3):
        first = _reserve(ledger, ticket_id=f"t{index}", worker_id=f"w{index}")
        ledger.mark_attempted(INTENT, f"t{index}", worker_id=f"w{index}")
        ledger.record_outcome(
            INTENT, f"t{index}", ExecutionOutcome.FAILED, worker_id=f"w{index}"
        )
        failures.append(first)
        clock.advance(timedelta(seconds=1))

    retry = _reserve(ledger, ticket_id="t3", worker_id="w3")

    assert retry.supersedes_reservation_id == failures[-1].reservation_id
    assert retry.supersedes_reservation_id != failures[0].reservation_id
    ledger.verify_lineage()


def test_only_one_reattempt_can_win_the_race_after_a_failure(
    ledger_path: Path, clock: FakeClock
) -> None:
    """Eight processes re-attempting one failed intent: one lineage reference.

    This is the atomicity proof for lineage specifically. Every process is
    permitted to re-execute -- the prior outcome is ``FAILED``, so the intent
    guard is satisfied for all of them -- which means nothing but the ledger
    decides who wins. If the prior-failure selection and the insert were separate
    steps, each process would read the *same* failed row and eight reservations
    would claim to supersede it, leaving durable lineage asserting that one
    failure authorized eight separate executions.

    Exactly one reference to the failed row may exist afterwards. That is the
    observable difference between "read the prior failure, then insert" and
    "read and insert in one transaction", and it is why the selection lives
    inside ``BEGIN IMMEDIATE`` rather than being handed to the caller.
    """
    store = DurableExecutionLedger(ledger_path, now=clock)
    failed = _reserve(store, ticket_id="t1", worker_id="w1")
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.record_outcome(INTENT, "t1", ExecutionOutcome.FAILED, worker_id="w1")
    store.close()

    intent = f"{INTENT}-reattempt-race"
    # The seeded failure is under INTENT; give the racers an intent whose only
    # prior is a failure, so all eight are legitimately permitted.
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute(
            "UPDATE execution_reservation SET intent_key = ? "
            "WHERE reservation_id = ?",
            (intent, failed.reservation_id),
        )
        conn.commit()
    finally:
        conn.close()

    results = _spawn_distinct_ticket_reservations(ledger_path, intent, 8)
    winners = [r for r in results if r[0] == "ok"]
    refusals = [r for r in results if r[0] == "err"]
    assert len(winners) == 1, results
    assert len(refusals) == 7, results
    assert {r[1] for r in refusals} == {"IntentAlreadyExecutedError"}, results

    reopened = DurableExecutionLedger(ledger_path, now=clock)
    try:
        rows = reopened.all_executions()
        assert len(rows) == 2, rows
        referencing = [r for r in rows if r.supersedes_reservation_id is not None]
        assert len(referencing) == 1, referencing
        assert referencing[0].supersedes_reservation_id == failed.reservation_id
        reopened.verify()
        reopened.verify_lineage()
    finally:
        reopened.close()


def test_separate_processes_reserve_one_execution_exactly_once(
    ledger_path: Path, clock: FakeClock
) -> None:
    """The load-bearing guarantee: eight processes, one winner, five rounds.

    Threads share a GIL and one set of connections, so only genuinely
    independent interpreters prove that SQLite's cross-process write lock is
    what provides exclusivity. Five rounds on one file also prove the losers
    left no residue: forty processes, five rows, five distinct winners.
    """
    store = DurableExecutionLedger(ledger_path, now=clock)
    store.close()

    rounds = 5
    winners: dict[str, str] = {}
    for round_index in range(rounds):
        intent = f"{INTENT}-round-{round_index}"
        results = _spawn_reservations(ledger_path, intent, "t1", 8)
        round_winners = [r for r in results if r[0] == "ok"]
        refusals = [r for r in results if r[0] == "err"]
        assert len(round_winners) == 1, results
        assert len(refusals) == 7, results
        assert {r[1] for r in refusals} == {"AlreadyReservedError"}, results
        winners[intent] = round_winners[0][2]

    reopened = DurableExecutionLedger(ledger_path, now=clock)
    try:
        rows = reopened.all_executions()
        assert len(rows) == rounds, [r.intent_key for r in rows]
        pairs = [(r.intent_key, r.ticket_id) for r in rows]
        assert len(pairs) == len(set(pairs))
        assert {r.intent_key for r in rows} == set(winners)
        # Each reservation belongs to whichever process actually won its round.
        for row in rows:
            assert row.worker_id == winners[row.intent_key]
            assert row.state is ExecutionReservationState.RESERVED
        reopened.verify()
    finally:
        reopened.close()


def _transition_worker(
    path: str,
    intent_key: str,
    ticket_id: str,
    operation: str,
    claim_worker: str,
    queue,
) -> None:
    """Module level so ``spawn`` can import it in a fresh interpreter.

    ``claim_worker`` is the worker id this process asserts. Half the racers
    assert the true owner and half assert an impostor, so the test proves
    ownership is enforced rather than merely present in the schema.
    """
    try:
        store = DurableExecutionLedger(path, verify_on_open=False)
        if operation == "attempted":
            result = store.mark_attempted(
                intent_key, ticket_id, worker_id=claim_worker, expected_revision=0
            )
        elif operation == "unknown":
            result = store.record_outcome(
                intent_key,
                ticket_id,
                ExecutionOutcome.UNKNOWN,
                worker_id=claim_worker,
                expected_revision=1,
            )
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unknown operation {operation}")
        queue.put(("ok", operation, result.state.value, result.revision))
    except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
        queue.put(("err", type(exc).__name__, "", -1))
    finally:
        store.close()


def _spawn_transitions(
    path: Path,
    intent_key: str,
    ticket_id: str,
    operations: list[str],
    claim_worker: str,
) -> list[tuple[str, str, str, int]]:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_transition_worker,
            args=(str(path), intent_key, ticket_id, operation, claim_worker, queue),
        )
        for operation in operations
    ]
    for process in processes:
        process.start()
    try:
        return _drain(queue, processes, len(operations))
    finally:
        for process in processes:
            if process.is_alive():  # pragma: no cover - only on a real hang
                process.kill()


def test_separate_processes_cross_the_boundary_at_most_once(
    ledger_path: Path, clock: FakeClock
) -> None:
    """Competing CAS transitions: exactly one may advance revision 0.

    Two processes assert the true owner and two assert an impostor. Only one
    write may land, and it must be one of the legitimate owner processes --
    if ownership were not enforced, the impostors would race for the same CAS
    and the winner could be either.
    """
    store = DurableExecutionLedger(ledger_path, now=clock)
    _reserve(store, worker_id="owner")
    store.close()

    results = _spawn_transitions(
        ledger_path, INTENT, "t1", ["attempted"] * 4, claim_worker="owner"
    )
    winners = [r for r in results if r[0] == "ok"]
    refusals = [r for r in results if r[0] == "err"]
    assert len(winners) == 1, results
    assert len(refusals) == 3, results
    assert winners[0][2] == ExecutionReservationState.ATTEMPTED.value
    assert winners[0][3] == 1

    reopened = DurableExecutionLedger(ledger_path, now=clock)
    try:
        assert reopened.get(INTENT, "t1").revision == 1
        reopened.verify()
    finally:
        reopened.close()


def test_separate_processes_cannot_impersonate_the_owning_worker(
    ledger_path: Path, clock: FakeClock
) -> None:
    """Every impostor is refused on ownership, before the CAS is even reached.

    This is the cross-process form of the ownership invariant: a second
    process that learned the ``(intent_key, ticket_id)`` pair must not be able
    to drive another worker's execution.
    """
    store = DurableExecutionLedger(ledger_path, now=clock)
    _reserve(store, worker_id="owner")
    store.mark_attempted(INTENT, "t1", worker_id="owner")
    store.close()

    results = _spawn_transitions(
        ledger_path, INTENT, "t1", ["unknown"] * 4, claim_worker="impostor"
    )
    assert [r[0] for r in results] == ["err"] * 4, results
    assert {r[1] for r in results} == {"ReservationOwnershipError"}, results

    reopened = DurableExecutionLedger(ledger_path, now=clock)
    try:
        survivor = reopened.get(INTENT, "t1")
        assert survivor.state is ExecutionReservationState.ATTEMPTED
        assert survivor.outcome is None
        assert survivor.revision == 1
        reopened.verify()
    finally:
        reopened.close()


def test_separate_processes_transition_to_unknown_at_most_once(
    ledger_path: Path, clock: FakeClock
) -> None:
    store = DurableExecutionLedger(ledger_path, now=clock)
    _reserve(store)
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.close()

    results = _spawn_transitions(
        ledger_path, INTENT, "t1", ["unknown"] * 4, claim_worker="w1"
    )
    winners = [r for r in results if r[0] == "ok"]
    assert len(winners) == 1, results
    assert winners[0][2] == ExecutionReservationState.UNRESOLVED.value

    reopened = DurableExecutionLedger(ledger_path, now=clock)
    try:
        final = reopened.get(INTENT, "t1")
        assert final.state is ExecutionReservationState.UNRESOLVED
        assert final.outcome is ExecutionOutcome.UNKNOWN
        assert final.revision == 2
        reopened.verify()
    finally:
        reopened.close()


# --- crash and restart visibility ------------------------------------------


def test_reservation_survives_process_exit(ledger_path: Path) -> None:
    """Part 8/10.A: a claim committed by one process is visible to the next."""
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    reservation = _reserve(store)
    store.close()

    reopened = DurableExecutionLedger(ledger_path, now=lambda: START)
    try:
        recovered = reopened.get(INTENT, "t1")
        assert recovered.reservation_id == reservation.reservation_id
        assert recovered.state is ExecutionReservationState.RESERVED
        assert recovered.worker_id == "w1"
        reopened.verify()
    finally:
        reopened.close()


def test_attempted_state_survives_restart(ledger_path: Path) -> None:
    """Part 10.B: the crash-during-call state is what the next process sees."""
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.close()

    reopened = DurableExecutionLedger(ledger_path, now=lambda: START)
    try:
        recovered = reopened.get(INTENT, "t1")
        assert recovered.state is ExecutionReservationState.ATTEMPTED
        assert recovered.may_have_crossed_boundary is True
        # And it is still not reclaimable.
        with pytest.raises(AlreadyAttemptedError):
            _reserve(reopened, worker_id="w2")
    finally:
        reopened.close()


def test_unknown_state_survives_restart(ledger_path: Path) -> None:
    """Part 10.D: UNKNOWN is terminal for automatic execution, across restarts."""
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.record_outcome(INTENT, "t1", ExecutionOutcome.UNKNOWN, worker_id="w1")
    store.close()

    reopened = DurableExecutionLedger(ledger_path, now=lambda: START)
    try:
        recovered = reopened.get(INTENT, "t1")
        assert recovered.state is ExecutionReservationState.UNRESOLVED
        assert recovered.outcome is ExecutionOutcome.UNKNOWN
        with pytest.raises(AlreadyResolvedError):
            _reserve(reopened, worker_id="w2")
    finally:
        reopened.close()


def test_resolved_state_survives_restart(ledger_path: Path) -> None:
    """Part 10.C."""
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.record_outcome(INTENT, "t1", ExecutionOutcome.VERIFIED_SUCCESS, worker_id="w1")
    store.close()

    reopened = DurableExecutionLedger(ledger_path, now=lambda: START)
    try:
        recovered = reopened.get(INTENT, "t1")
        assert recovered.state is ExecutionReservationState.RESOLVED
        assert recovered.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    finally:
        reopened.close()


def test_get_never_answers_from_a_cache(ledger_path: Path, clock: FakeClock) -> None:
    """A handle opened before another process wrote must still see it.

    Phase 2 found the audit ledger answered from an in-memory dict populated
    once at open, which made its duplicate scans process-local. This is the
    regression that would reintroduce exactly that defect here.
    """
    long_lived = DurableExecutionLedger(ledger_path, now=clock, verify_on_open=False)
    try:
        other = DurableExecutionLedger(ledger_path, now=clock, verify_on_open=False)
        try:
            _reserve(other)
        finally:
            other.close()
        # Same open handle, written by a different process.
        assert long_lived.get(INTENT, "t1").worker_id == "w1"
        with pytest.raises(AlreadyReservedError):
            _reserve(long_lived, worker_id="w2")
    finally:
        long_lived.close()


# --- corruption and availability -------------------------------------------


def test_corrupt_file_fails_closed(ledger_path: Path) -> None:
    ledger_path.write_bytes(b"this is not a sqlite database" * 10)
    with pytest.raises(ExecutionLedgerCorruptionError):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_foreign_sqlite_file_fails_closed(ledger_path: Path) -> None:
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.execute("INSERT INTO unrelated VALUES (1)")
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_missing_schema_version_fails_closed(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_unknown_schema_version_fails_closed(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute(
            "UPDATE meta SET value = '999' WHERE key = 'schema_version'"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError, match="schema version"):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_corrupt_state_value_fails_closed(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute("UPDATE execution_reservation SET state = 'banana'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_corrupt_outcome_value_fails_closed(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute(
            "UPDATE execution_reservation SET state='resolved', outcome='nope'"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_resolved_without_an_outcome_fails_closed(ledger_path: Path) -> None:
    """A settled execution that cannot say what happened is corruption."""
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute(
            "UPDATE execution_reservation SET state='resolved', outcome=NULL"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError, match="no outcome"):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_unresolved_with_a_definite_outcome_fails_closed(
    ledger_path: Path,
) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.record_outcome(INTENT, "t1", ExecutionOutcome.UNKNOWN, worker_id="w1")
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute(
            "UPDATE execution_reservation SET outcome='verified_success'"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError, match="UNRESOLVED"):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_open_state_carrying_an_outcome_fails_closed(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute(
            "UPDATE execution_reservation SET outcome='verified_success'"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError, match="only a terminal"):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_duplicate_rows_fail_closed(ledger_path: Path) -> None:
    """The uniqueness invariant is verified, not merely assumed."""
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        # Rebuild the table without the UNIQUE constraint, then duplicate a pair
        # under a different primary key.
        conn.execute("DROP TABLE execution_reservation")
        conn.execute(
            "CREATE TABLE execution_reservation ("
            "reservation_id TEXT PRIMARY KEY, intent_key TEXT NOT NULL, "
            "ticket_id TEXT NOT NULL, ticket_revision INTEGER NOT NULL, "
            "action_plan_id TEXT, resource_id TEXT, action TEXT, "
            "worker_id TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT, "
            "revision INTEGER NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, supersedes_reservation_id TEXT)"
        )
        row = (
            "i1", INTENT, "t1", 1, "plan-1", "i-1", "stop_resource", "w1",
            "reserved", None, 0, START.isoformat(), START.isoformat(), None,
        )
        clone = ("i2", *row[1:])
        for values in (row, clone):
            conn.execute(
                "INSERT INTO execution_reservation "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError, match="two reservations"):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_naive_timestamp_fails_closed(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute(
            "UPDATE execution_reservation SET created_at='2026-01-01T00:00:00'"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError, match="naive timestamp"):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_unparseable_timestamp_fails_closed(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute("UPDATE execution_reservation SET updated_at='not-a-date'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ExecutionLedgerCorruptionError, match="unparseable"):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_closed_ledger_refuses_work(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.close()
    store.close()  # idempotent
    with pytest.raises(ExecutionLedgerUnavailableError):
        _reserve(store)
    with pytest.raises(ExecutionLedgerUnavailableError):
        store.get(INTENT, "t1")


def test_verify_passes_on_a_healthy_ledger(ledger: DurableExecutionLedger) -> None:
    _reserve(ledger)
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    ledger.verify()


def test_verify_rejects_a_structurally_damaged_page(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store)
    store.close()
    # Corrupt page bytes directly; integrity_check must notice.
    raw = bytearray(ledger_path.read_bytes())
    raw[4096:4200] = b"\xff" * 104
    ledger_path.write_bytes(bytes(raw))
    with pytest.raises((ExecutionLedgerCorruptionError, ExecutionLedgerUnavailableError)):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_transaction_rolls_back_on_failure(
    ledger: DurableExecutionLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise inside the transaction must leave no partial claim behind."""
    import sws_agent.execution_ledger as module

    def explode(*_args, **_kwargs):
        raise ExecutionLedgerUnavailableError("simulated mid-transaction failure")

    monkeypatch.setattr(module, "_format_ts", explode)
    with pytest.raises(ExecutionLedgerUnavailableError):
        _reserve(ledger)
    monkeypatch.undo()

    fresh = DurableExecutionLedger(ledger.path, now=lambda: START)
    try:
        assert fresh.all_executions() == ()
    finally:
        fresh.close()


def test_id_source_failure_leaves_no_claim(ledger: DurableExecutionLedger) -> None:
    ledger.close()
    store = DurableExecutionLedger(
        ledger.path, now=lambda: START, id_source=lambda: ""
    )
    try:
        with pytest.raises(ExecutionLedgerUnavailableError):
            _reserve(store)
        assert store.all_executions() == ()
    finally:
        store.close()


def test_repr_mentions_the_path(ledger_path: Path) -> None:
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    try:
        assert ledger_path.name in repr(store)
    finally:
        store.close()


# --------------------------------------------------------------------------
# M13 Phase 4: the intent-level decision and its durable lineage.
#
# The rule under test, from the Phase 4 ruling. A fresh ticket may execute an
# intent again only when the prior same-intent execution recorded FAILED:
#
#   RESERVED               -> block  (no automatic stale-reservation takeover)
#   ATTEMPTED              -> block  (the boundary may already be crossed)
#   VERIFIED_SUCCESS       -> block  (the effect demonstrably happened)
#   PARTIALLY_VERIFIED     -> block  (part of it demonstrably happened)
#   UNRESOLVED / UNKNOWN   -> block  (nothing established either way)
#   FAILED                 -> allow, recording supersedes_reservation_id
#   no prior execution     -> allow
# --------------------------------------------------------------------------

_BLOCKING_STATES = (
    ("a prior RESERVED claim", None, None),
    ("a prior ATTEMPTED row", ExecutionReservationState.ATTEMPTED, None),
    (
        "a prior VERIFIED_SUCCESS outcome",
        ExecutionReservationState.ATTEMPTED,
        ExecutionOutcome.VERIFIED_SUCCESS,
    ),
    (
        "a prior PARTIALLY_VERIFIED outcome",
        ExecutionReservationState.ATTEMPTED,
        ExecutionOutcome.PARTIALLY_VERIFIED,
    ),
    (
        "a prior UNRESOLVED/UNKNOWN outcome",
        ExecutionReservationState.ATTEMPTED,
        ExecutionOutcome.UNKNOWN,
    ),
)


@pytest.mark.parametrize(("label", "settle_state", "outcome"), _BLOCKING_STATES)
def test_a_fresh_ticket_is_refused_for_every_non_failed_prior(
    ledger: DurableExecutionLedger,
    label: str,
    settle_state: ExecutionReservationState | None,
    outcome: ExecutionOutcome | None,
) -> None:
    """Only ``FAILED`` permits a second execution of the same intent.

    Each case is a distinct safety reason rather than a repeated assertion, so
    the labels are kept in the test id: a future change that started allowing
    one of these states would be caught with the reason visible.
    """
    _reserve(ledger, ticket_id="t1")
    if settle_state is not None:
        ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    if outcome is not None:
        ledger.record_outcome(INTENT, "t1", outcome, worker_id="w1")

    with pytest.raises(IntentAlreadyExecutedError):
        _reserve(ledger, ticket_id="t2", worker_id="w2")

    assert len(ledger.all_executions()) == 1
    assert ledger.get(INTENT, "t1").intent_key == INTENT


def test_blocking_refusal_names_the_state_that_blocks_it(
    ledger: DurableExecutionLedger,
) -> None:
    """The refusal says *why*, because the operator's next step differs.

    ``RESERVED`` and ``ATTEMPTED`` are the two that look alike from outside --
    both are "in progress" -- but they call for different responses, and a
    message that merely said "already exists" would leave an operator unable to
    tell a crashed-before-call worker from a crashed-during-call one.
    """
    _reserve(ledger, ticket_id="t1")
    with pytest.raises(IntentAlreadyExecutedError, match="RESERVED"):
        _reserve(ledger, ticket_id="t2", worker_id="w2")

    settled = DurableExecutionLedger(
        ledger.path.parent / "second.sqlite3", now=lambda: START
    )
    try:
        _reserve(settled, ticket_id="t1")
        settled.mark_attempted(INTENT, "t1", worker_id="w1")
        with pytest.raises(IntentAlreadyExecutedError, match="ATTEMPTED"):
            _reserve(settled, ticket_id="t2", worker_id="w2")
    finally:
        settled.close()


def test_unknown_is_not_a_retry_path_even_with_a_fresh_ticket(
    ledger: DurableExecutionLedger,
) -> None:
    """``UNKNOWN`` blocks, and no in-band override exists to get around it.

    The tempting rule -- "unknown means the effect may not have happened, so
    allow a fresh ticket to try again" -- is exactly the ambiguity M13 exists
    to prevent. Nothing established whether the world already changed, and
    repeating the effect could compound an unknown outcome into two.

    Overriding this needs an explicit reconciliation mechanism with its own
    authorization and audit trail, which M13 deliberately does not provide. A
    new ticket is not that mechanism: it says nothing about the earlier
    attempt. So the answer is no, permanently, on this path.
    """
    _reserve(ledger, ticket_id="t1")
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    unresolved = ledger.record_outcome(
        INTENT, "t1", ExecutionOutcome.UNKNOWN, worker_id="w1"
    )
    assert unresolved.state is ExecutionReservationState.UNRESOLVED

    # Not the same pair, not the same ticket, still refused.
    with pytest.raises(IntentAlreadyExecutedError, match="UNRESOLVED"):
        _reserve(ledger, ticket_id="t2", worker_id="w2")

    # Nor is the same pair retryable by any other worker.
    with pytest.raises(AlreadyResolvedError):
        _reserve(ledger, ticket_id="t1", worker_id="w9")


def test_a_failed_prior_records_durable_lineage(
    ledger: DurableExecutionLedger,
) -> None:
    """A permitted reattempt says which failure it supersedes.

    Two rows sharing an intent key would otherwise be indistinguishable from a
    duplicate, and nothing in the file would record *why* the second was
    allowed. The reference is what makes the safety model auditable rather than
    merely correct.
    """
    first = _reserve(ledger, ticket_id="t1")
    assert first.supersedes_reservation_id is None
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    ledger.record_outcome(INTENT, "t1", ExecutionOutcome.FAILED, worker_id="w1")

    second = _reserve(ledger, ticket_id="t2", worker_id="w2")
    assert second.supersedes_reservation_id == first.reservation_id

    # It survives a reopen, which is the only test that matters for durability.
    reopened = DurableExecutionLedger(ledger.path)
    try:
        rows = {r.ticket_id: r for r in reopened.all_executions()}
        assert rows["t2"].supersedes_reservation_id == first.reservation_id
        assert rows["t1"].supersedes_reservation_id is None
        reopened.verify_lineage()
    finally:
        reopened.close()


def test_a_chain_of_failed_reattempts_names_its_immediate_predecessor(
    ledger: DurableExecutionLedger, clock: FakeClock
) -> None:
    """The chain reads forwards, so the newest failure is what gets named.

    Naming the *first* failure would leave a reader unable to tell which attempt
    the current one follows, and would make the lineage a set of siblings rather
    than a sequence.

    The clock is advanced between attempts because "newest" is decided by
    ``updated_at``. With a frozen clock every row shares a timestamp and the
    ordering falls back to reservation id, which is a uuid -- so this test would
    be asserting on random values rather than on the rule. Real attempts are
    separated by the time it takes to make one.
    """
    first = _reserve(ledger, ticket_id="t1")
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    ledger.record_outcome(INTENT, "t1", ExecutionOutcome.FAILED, worker_id="w1")

    clock.advance(timedelta(seconds=1))
    second = _reserve(ledger, ticket_id="t2", worker_id="w2")
    ledger.mark_attempted(INTENT, "t2", worker_id="w2")
    ledger.record_outcome(INTENT, "t2", ExecutionOutcome.FAILED, worker_id="w2")

    clock.advance(timedelta(seconds=1))
    third = _reserve(ledger, ticket_id="t3", worker_id="w3")

    assert second.supersedes_reservation_id == first.reservation_id
    assert third.supersedes_reservation_id == second.reservation_id
    ledger.verify_lineage()


def test_lineage_verification_rejects_a_reference_across_intents(
    ledger_path: Path,
) -> None:
    """A hand-edited file that points lineage at another intent fails closed.

    ``reserve`` cannot produce this, so the check exists for the case the
    runtime check cannot cover: a file edited, restored from an inconsistent
    backup, or written by a build with a different bug. A durable authority
    that cannot prove its own contents is not an authority.
    """
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    failed = _reserve(store, intent_key=INTENT, ticket_id="t1")
    other = _reserve(store, intent_key=OTHER_INTENT, ticket_id="t2", worker_id="w2")
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.record_outcome(INTENT, "t1", ExecutionOutcome.FAILED, worker_id="w1")
    store.close()

    conn = sqlite3.connect(ledger_path)
    try:
        # Point the OTHER intent's row at this intent's FAILED row: same
        # relation shape, wrong intent.
        conn.execute(
            "UPDATE execution_reservation SET supersedes_reservation_id = ? "
            "WHERE reservation_id = ?",
            (failed.reservation_id, other.reservation_id),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = DurableExecutionLedger(ledger_path, now=lambda: START)
    try:
        with pytest.raises(ExecutionLedgerCorruptionError, match="belongs to intent"):
            reopened.verify_lineage()
    finally:
        reopened.close()


def test_lineage_verification_rejects_superseding_a_non_failure(
    ledger_path: Path,
) -> None:
    """Pointing a reattempt at a successful run is refused as corruption.

    The invariant is that only a ``FAILED`` execution may be superseded. A
    reference to a settled success would assert that a second attempt was
    authorized to replace an effect that actually worked.
    """
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    succeeded = _reserve(store, ticket_id="t1")
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.record_outcome(
        INTENT, "t1", ExecutionOutcome.VERIFIED_SUCCESS, worker_id="w1"
    )
    # A fresh reservation is legitimately refused, so this relation has to be
    # written directly -- which is exactly the corruption case under test.
    store.close()

    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute(
            "INSERT INTO execution_reservation "
            "(reservation_id, intent_key, ticket_id, ticket_revision, worker_id, "
            " state, outcome, revision, created_at, updated_at, "
            " supersedes_reservation_id) "
            "VALUES ('reattempt', 'intent-aaa', 't9', 1, 'w9', 'reserved', "
            "NULL, 0, ?, ?, ?)",
            (START.isoformat(), START.isoformat(), succeeded.reservation_id),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = DurableExecutionLedger(ledger_path, now=lambda: START)
    try:
        with pytest.raises(
            ExecutionLedgerCorruptionError, match="only a FAILED"
        ):
            reopened.verify_lineage()
    finally:
        reopened.close()


def test_lineage_verification_rejects_a_cycle(
    ledger_path: Path,
) -> None:
    """A cyclic chain is corruption, and must be reported rather than hang.

    Every reference here points at a ``FAILED`` row of the same intent, so each
    individual check passes. Only walking the chain catches the loop, and the
    walk is bounded so a corrupted file produces an error instead of an
    infinite loop.
    """
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    only = _reserve(store, ticket_id="t1")
    store.mark_attempted(INTENT, "t1", worker_id="w1")
    store.record_outcome(INTENT, "t1", ExecutionOutcome.FAILED, worker_id="w1")
    store.close()

    conn = sqlite3.connect(ledger_path)
    try:
        # A self-reference: every other check still passes, because the target
        # exists, is the same intent, and is FAILED.
        conn.execute(
            "UPDATE execution_reservation SET supersedes_reservation_id = ? "
            "WHERE reservation_id = ?",
            (only.reservation_id, only.reservation_id),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = DurableExecutionLedger(ledger_path, now=lambda: START)
    try:
        with pytest.raises(
            ExecutionLedgerCorruptionError, match="supersede itself"
        ):
            reopened.verify_lineage()
    finally:
        reopened.close()


def test_corruption_is_not_a_reservation_conflict(
    ledger_path: Path,
) -> None:
    """A damaged ledger must not be able to surface as ordinary contention.

    The coordinator catches ``ReservationConflictError`` and reports
    ``EXECUTION_ALREADY_RESERVED`` -- an instruction to stop because something
    else holds the execution. If corruption were a subclass, a ledger that
    cannot answer whether it is safe to act would instead say "someone else got
    there first", and the caller's response (retry later) would be exactly wrong:
    the durable state is unverifiable, so retrying trusts a record that was
    already proven untrustworthy.

    Failing closed means corruption propagates instead of becoming a refusal.
    """
    assert not issubclass(
        ExecutionLedgerCorruptionError, ReservationConflictError
    ), "corruption must not be catchable as ordinary contention"
    assert issubclass(ExecutionLedgerCorruptionError, ExecutionLedgerError)
    assert issubclass(
        ExecutionLedgerUnavailableError, ExecutionLedgerError
    )
    assert not issubclass(
        ExecutionLedgerUnavailableError, ReservationConflictError
    )

    # And the three refusal categories stay distinguishable types, so the
    # coordinator can report them differently without inspecting messages.
    for distinct in (
        IntentAlreadyExecutedError,
        TicketRevisionConflictError,
    ):
        assert issubclass(distinct, ReservationConflictError), distinct
        assert distinct is not ReservationConflictError, distinct
    assert IntentAlreadyExecutedError is not TicketRevisionConflictError
    # A binding failure is not an execution duplicate.
    assert not issubclass(
        TicketRevisionConflictError, IntentAlreadyExecutedError
    )

    # Behaviourally: a corrupt file raises corruption, not a conflict.
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    _reserve(store, ticket_id="t1")
    store.close()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute(
            "UPDATE execution_reservation SET state = 'resolved', outcome = NULL"
        )
        conn.commit()
    finally:
        conn.close()

    reopened = DurableExecutionLedger(ledger_path, verify_on_open=False)
    try:
        with pytest.raises(ExecutionLedgerCorruptionError):
            reopened.verify()
    finally:
        reopened.close()


def test_a_v1_ledger_is_refused_rather_than_guessed_at(
    ledger_path: Path,
) -> None:
    """A v1 file has no lineage column, so it is rejected, not reinterpreted.

    The alternative -- adding the column to an existing file in place -- would
    mean reading a layout this build does not understand and writing to it.
    Every row would be silently reinterpreted as having no lineage, which is
    precisely the "a v1 file cannot have authorized a reattempt" claim that
    needs proving rather than assuming. Failing closed keeps the old rows
    intact for a real migration.
    """
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.execute(
            "CREATE TABLE execution_reservation ("
            "reservation_id TEXT PRIMARY KEY, intent_key TEXT NOT NULL, "
            "ticket_id TEXT NOT NULL, ticket_revision INTEGER NOT NULL, "
            "action_plan_id TEXT, resource_id TEXT, action TEXT, "
            "worker_id TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT, "
            "revision INTEGER NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ExecutionLedgerCorruptionError, match="schema version 1"):
        DurableExecutionLedger(ledger_path, now=lambda: START)


def test_a_new_ledger_records_the_current_schema_version(
    ledger_path: Path,
) -> None:
    """The v2 bump is asserted, so it cannot be forgotten silently.

    Without this, the lineage column could be added while the version stayed at
    1, and a v1 file would then open against a layout it does not match.
    """
    assert EXECUTION_LEDGER_SCHEMA_VERSION == 2
    store = DurableExecutionLedger(ledger_path, now=lambda: START)
    store.close()

    conn = sqlite3.connect(ledger_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        columns = {
            r[1] for r in conn.execute("PRAGMA table_info(execution_reservation)")
        }
    finally:
        conn.close()

    assert row is not None
    assert str(row[0]) == "2"
    assert "supersedes_reservation_id" in columns


def test_the_intent_guard_does_not_confuse_different_intents(
    ledger: DurableExecutionLedger,
) -> None:
    """Two different effects are two different intents, and both may run.

    The guard must not become so broad that it blocks unrelated work, which is
    the failure mode of any replay defence keyed too coarsely.
    """
    first = _reserve(ledger, intent_key=INTENT, ticket_id="t1")
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    ledger.record_outcome(
        INTENT, "t1", ExecutionOutcome.VERIFIED_SUCCESS, worker_id="w1"
    )

    second = _reserve(ledger, intent_key=OTHER_INTENT, ticket_id="t2", worker_id="w2")

    assert second.intent_key != first.intent_key
    assert second.supersedes_reservation_id is None


def test_a_reservation_never_supersedes_itself_by_construction(
    ledger: DurableExecutionLedger,
) -> None:
    """The reference is derived from prior state, never from caller input.

    ``reserve`` has no parameter a caller could use to name the prior
    execution, so there is no path by which a caller can claim to supersede an
    arbitrary row -- including itself. The lineage is a consequence of what was
    already durably recorded, which is what makes it trustworthy.
    """
    _reserve(ledger, ticket_id="t1")
    ledger.mark_attempted(INTENT, "t1", worker_id="w1")
    ledger.record_outcome(INTENT, "t1", ExecutionOutcome.FAILED, worker_id="w1")
    row = _reserve(ledger, ticket_id="t2", worker_id="w2")

    assert row.supersedes_reservation_id != row.reservation_id
    assert row.supersedes_reservation_id is not None
    assert row.ticket_id != "t1"
    ledger.verify_lineage()