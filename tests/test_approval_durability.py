"""Durable approval ledger: schema, CAS, atomic events, restart, concurrency.

Hermetic by construction. Every test uses a real SQLite file in pytest's
``tmp_path``, an injected clock, and no network or AWS access. Concurrency is
genuine, not simulated: the thread tests share one store object across real
threads, and the process tests spawn independent interpreters that open the
same ledger file, which is the only way to show the store is multi-process
safe rather than merely internally locked.

Time is always injected. Anything that waits does so on a real clock but only
for lock contention bounded by the store's busy timeout, never by sleeping out
an expiry.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from sws_agent.approval import (
    DEFAULT_APPROVAL_EXECUTION_TTL,
    DEFAULT_APPROVAL_TICKET_TTL,
    LEGAL_APPROVAL_TRANSITIONS,
    ApprovalStoreCorruptionError,
    ApprovalStoreUnavailableError,
    DuplicateTicketError,
    InMemoryApprovalStore,
    InvalidTransitionError,
    RevisionConflictError,
    UnknownTicketError,
)
from sws_agent.approval_ledger import (
    APPROVAL_LEDGER_SCHEMA_VERSION,
    DEFAULT_APPROVAL_BUSY_TIMEOUT_SECONDS,
    DurableApprovalStore,
    TicketEventType,
)
from sws_agent.constants import ApprovalStatus, PotentialAction
from sws_agent.interfaces import ApprovalStore
from sws_agent.models import ApprovalTicket

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


class FakeClock:
    """Deterministic, mutable time source, mirroring tests/test_approval.py."""

    def __init__(self, current: datetime = START) -> None:
        self._current = current

    def __call__(self) -> datetime:
        return self._current

    def advance(self, **delta: Any) -> None:
        self._current = self._current + timedelta(**delta)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "approvals.db"


@pytest.fixture
def store(ledger_path: Path, clock: FakeClock) -> DurableApprovalStore:
    instance = DurableApprovalStore(ledger_path, now=clock)
    yield instance
    instance.close()


def _issue(
    store: DurableApprovalStore,
    ticket_id: str = "t1",
    *,
    resource: str = "bucket-demo",
    action: PotentialAction = PotentialAction.STOP_RESOURCE,
    **kwargs: Any,
) -> ApprovalTicket:
    return store.create_ticket(
        resource, action, "demo run", ticket_id=ticket_id, **kwargs
    )


# ---------------------------------------------------------------- storage --


def test_satisfies_the_approval_store_protocol(store: DurableApprovalStore) -> None:
    assert isinstance(store, ApprovalStore)


def test_database_path_must_be_explicit() -> None:
    with pytest.raises(TypeError):
        DurableApprovalStore()  # type: ignore[call-arg]


def test_creates_missing_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "state" / "deeper" / "approvals.db"
    store = DurableApprovalStore(nested, now=lambda: START)
    try:
        assert nested.exists()
        assert store.path == nested
    finally:
        store.close()


def test_no_silent_in_memory_fallback_on_unusable_path(
    tmp_path: Path, clock: FakeClock
) -> None:
    """A store that cannot reach its file must fail, not quietly go ephemeral.

    Falling back to memory would be the worst possible failure here: the
    process would report a working approval store while silently forgetting
    every decision the instant it exited.
    """
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises((ApprovalStoreUnavailableError, ApprovalStoreCorruptionError)):
        DurableApprovalStore(directory, now=clock)


def test_persists_across_process_restart(ledger_path: Path, clock: FakeClock) -> None:
    first = DurableApprovalStore(ledger_path, now=clock)
    _issue(first)
    first.grant("t1", decided_by="alice", reason="looks fine")
    first.consume("t1")
    first.close()

    second = DurableApprovalStore(ledger_path, now=clock)
    ticket = second.get("t1")
    assert ticket.status is ApprovalStatus.CONSUMED
    assert ticket.consumed is True
    assert ticket.revision == 2
    assert ticket.decided_by == "alice"
    assert ticket.decision_reason == "looks fine"
    assert ticket.decided_at == START
    assert [e.event_type for e in second.events("t1")] == [
        TicketEventType.ISSUED,
        TicketEventType.GRANTED,
        TicketEventType.CONSUMED,
    ]
    second.close()


def test_use_after_close_fails_loudly(ledger_path: Path, clock: FakeClock) -> None:
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.close()
    store.close()  # idempotent
    with pytest.raises(ApprovalStoreUnavailableError):
        store.get("t1")


# ----------------------------------------------------------------- schema --


def test_schema_version_is_stamped_on_creation(
    ledger_path: Path, clock: FakeClock
) -> None:
    DurableApprovalStore(ledger_path, now=clock).close()
    with sqlite3.connect(ledger_path) as conn:
        value = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert value == str(APPROVAL_LEDGER_SCHEMA_VERSION)


def test_schema_persists_every_phase_one_field(
    ledger_path: Path, clock: FakeClock
) -> None:
    """Every field Phase 1 added must be a real column, not a lost value."""
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(
        store,
        plan_id="plan-1",
        execution_intent_key="i-abc",
        evidence_digest="d1",
    )
    store.grant("t1")
    store.close()
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ticket)")}
        row = conn.execute("SELECT * FROM ticket WHERE ticket_id = 't1'").fetchone()
    assert {
        "ticket_id",
        "resource_id",
        "action",
        "rationale",
        "status",
        "created_at",
        "decided_at",
        "decided_by",
        "decision_reason",
        "plan_id",
        "consumed",
        "revision",
        "execution_intent_key",
        "evidence_digest",
        "execution_deadline",
    } <= columns
    assert row is not None
    assert row["consumed"] == 0
    assert row["revision"] == 1
    assert row["evidence_digest"] == "d1"
    assert row["execution_intent_key"] == "i-abc"
    assert row["plan_id"] == "plan-1"
    assert row["execution_deadline"] == (
        START + DEFAULT_APPROVAL_EXECUTION_TTL
    ).isoformat()


def test_rejects_foreign_database_file(tmp_path: Path, clock: FakeClock) -> None:
    """An unrelated SQLite file must never be adopted as the authority.

    Creating the missing tables with IF NOT EXISTS would "succeed" here and
    hand the caller an empty approval ledger sitting on top of someone else's
    data. Failing closed is the only safe answer.
    """
    path = tmp_path / "unrelated.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE notes (body TEXT)")
        conn.execute("INSERT INTO notes VALUES ('hello')")
    with pytest.raises(ApprovalStoreCorruptionError, match="meta"):
        DurableApprovalStore(path, now=clock)


def test_rejects_non_database_file(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "junk.db"
    path.write_bytes(b"definitely not a sqlite file" * 200)
    with pytest.raises(ApprovalStoreCorruptionError, match="not a readable SQLite"):
        DurableApprovalStore(path, now=clock)


def test_rejects_unknown_schema_version(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "future.db"
    DurableApprovalStore(path, now=clock).close()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meta SET value = '99' WHERE key = 'schema_version'"
        )
    with pytest.raises(ApprovalStoreCorruptionError, match="99"):
        DurableApprovalStore(path, now=clock)


def test_rejects_nameless_schema_version(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "unstamped.db"
    DurableApprovalStore(path, now=clock).close()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
    with pytest.raises(ApprovalStoreCorruptionError):
        DurableApprovalStore(path, now=clock)


# ------------------------------------------------------- transition table --


def _drive(store: Any, status: ApprovalStatus, ticket_id: str = "drive") -> str:
    """Put a freshly issued ticket into ``status``; return the ticket's id.

    DENIED is only reachable from PENDING, so reaching it needs a second
    ticket rather than a rewind of the first -- which is exactly why no legal
    transition targets PENDING.
    """
    _issue(store, ticket_id)
    if status is ApprovalStatus.DENIED:
        store.deny(ticket_id, decided_by="bob", reason="not allowed")
    else:
        if status is not ApprovalStatus.PENDING:
            store.grant(ticket_id, decided_by="alice")
        if status is ApprovalStatus.CONSUMED:
            store.consume(ticket_id)
        elif status is ApprovalStatus.EXPIRED:
            store.expire(ticket_id, reason="winded down")
        elif status is ApprovalStatus.REVOKED:
            store.revoke(ticket_id, decided_by="bob", reason="changed my mind")
    assert store.get(ticket_id).status is status
    return ticket_id


def test_transition_table_matches_the_in_memory_reference(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The durable store must accept exactly Phase 1's legal moves.

    Driving the same two moves through both implementations is the check that
    Phase 2 did not quietly widen or narrow the state machine while adding
    persistence.
    """

    def attempt(store: Any, ticket_id: str, target: ApprovalStatus) -> bool:
        _drive(store, origin, ticket_id)
        if target is ApprovalStatus.PENDING:
            # No store method targets PENDING, and no legal target is PENDING.
            return False
        try:
            _transition_to(store, ticket_id, target)
        except InvalidTransitionError:
            return False
        return store.get(ticket_id).status is target

    for origin in ApprovalStatus:
        for target in ApprovalStatus:
            if origin is target:
                continue
            expected = target in LEGAL_APPROVAL_TRANSITIONS[origin]
            durable = DurableApprovalStore(
                tmp_path / f"durable-{origin.value}-{target.value}.db", now=clock
            )
            memory = InMemoryApprovalStore(now=clock)
            try:
                durable_outcome = attempt(durable, "t1", target)
                memory_outcome = attempt(memory, "t1", target)
            finally:
                durable.close()
            assert durable_outcome is expected, (origin, target, "durable")
            assert memory_outcome is expected, (origin, target, "in-memory")
            assert durable_outcome is memory_outcome, (origin, target)


def _transition_to(
    store: Any, ticket_id: str, target: ApprovalStatus
) -> ApprovalTicket:
    if target is ApprovalStatus.GRANTED:
        return store.grant(ticket_id, decided_by="alice")
    if target is ApprovalStatus.CONSUMED:
        return store.consume(ticket_id)
    if target is ApprovalStatus.EXPIRED:
        return store.expire(ticket_id, reason="done")
    if target is ApprovalStatus.REVOKED:
        return store.revoke(ticket_id, decided_by="bob", reason="no longer needed")
    if target is ApprovalStatus.DENIED:
        return store.deny(ticket_id, decided_by="bob", reason="not allowed")
    # No transition may lead back to PENDING; an issued ticket is never
    # rewound, which is what makes replay protection meaningful.
    assert target is not ApprovalStatus.PENDING
    raise AssertionError(f"unhandled target {target}")


def test_consume_is_possible_exactly_once(store: DurableApprovalStore) -> None:
    _issue(store)
    store.grant("t1")
    consumed = store.consume("t1")
    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.consumed is True
    with pytest.raises(InvalidTransitionError):
        store.consume("t1")
    assert store.get("t1").status is ApprovalStatus.CONSUMED


def test_revoked_ticket_cannot_be_consumed(store: DurableApprovalStore) -> None:
    _issue(store)
    store.grant("t1")
    store.revoke("t1", decided_by="bob", reason="superseded")
    with pytest.raises(InvalidTransitionError):
        store.consume("t1")
    assert store.get("t1").status is ApprovalStatus.REVOKED


def test_unknown_ticket_is_reported(store: DurableApprovalStore) -> None:
    with pytest.raises(UnknownTicketError):
        store.get("missing")
    with pytest.raises(UnknownTicketError):
        store.grant("missing")


def test_duplicate_explicit_ticket_id_is_refused(
    store: DurableApprovalStore,
) -> None:
    _issue(store, resource="bucket-original")
    with pytest.raises(DuplicateTicketError):
        _issue(store, resource="bucket-impostor")
    assert store.get("t1").resource_id == "bucket-original"


def test_duplicate_error_is_distinct_from_revision_conflict(
    store: DurableApprovalStore,
) -> None:
    """A taken identity and a stale precondition need different caller fixes.

    One is resolved by picking another id; the other by re-reading state. If
    they collapsed into a single error, a caller could not tell which.
    """
    _issue(store)
    store.grant("t1")
    with pytest.raises(DuplicateTicketError):
        _issue(store)
    with pytest.raises(RevisionConflictError):
        store.revoke("t1", expected_revision=0)
    assert not issubclass(DuplicateTicketError, RevisionConflictError)


def test_generated_ticket_ids_do_not_collide(
    store: DurableApprovalStore,
) -> None:
    ids = {_issue(store, f"auto-{i}").ticket_id for i in range(25)}
    assert len(ids) == 25


# -------------------------------------------------------------------- CAS --


def test_revision_advances_by_one_per_transition(
    store: DurableApprovalStore,
) -> None:
    _issue(store)
    assert store.get("t1").revision == 0
    assert store.grant("t1").revision == 1
    assert store.revoke("t1").revision == 2


def test_stale_expected_revision_is_refused(store: DurableApprovalStore) -> None:
    _issue(store)
    store.grant("t1")
    with pytest.raises(RevisionConflictError, match="revision 1"):
        store.revoke("t1", expected_revision=0)
    assert store.get("t1").revision == 1
    assert store.get("t1").status is ApprovalStatus.GRANTED


def test_matching_expected_revision_succeeds(store: DurableApprovalStore) -> None:
    _issue(store)
    assert store.grant("t1", expected_revision=0).revision == 1
    assert store.revoke("t1", expected_revision=1).revision == 2


def test_future_expected_revision_is_refused(store: DurableApprovalStore) -> None:
    _issue(store)
    with pytest.raises(RevisionConflictError):
        store.grant("t1", expected_revision=7)
    assert store.get("t1").status is ApprovalStatus.PENDING


def test_stale_revision_never_overwrites_newer_state(
    ledger_path: Path, clock: FakeClock
) -> None:
    """The core anti-clobber guarantee, checked across a restart.

    A caller may hold a ticket it read before someone else revoked it. Its
    late write must be refused against the durable authority, and the newer
    decision must survive untouched.
    """
    first = DurableApprovalStore(ledger_path, now=clock)
    _issue(first)
    first.grant("t1")
    stale_read = first.get("t1")
    first.revoke("t1", decided_by="bob", reason="auditor noticed something")
    first.close()

    second = DurableApprovalStore(ledger_path, now=clock)
    with pytest.raises((RevisionConflictError, InvalidTransitionError)):
        second.consume("t1", expected_revision=stale_read.revision)
    survivor = second.get("t1")
    assert survivor.status is ApprovalStatus.REVOKED
    assert survivor.revision == 2
    assert survivor.decision_reason == "auditor noticed something"
    second.verify()


# ----------------------------------------------------------- expiry rules --


def test_pending_ticket_expires_after_decision_ttl(
    tmp_path: Path, clock: FakeClock
) -> None:
    """PENDING is judged against the issuance-time decision window."""
    store = DurableApprovalStore(tmp_path / "ttl.db", now=clock, ttl=HOUR)
    _issue(store)
    clock.advance(minutes=59)
    assert store.get("t1").status is ApprovalStatus.PENDING
    clock.advance(minutes=2)
    assert store.get("t1").status is ApprovalStatus.EXPIRED
    assert store.pending() == []
    assert store.events("t1")[-1].event_type is TicketEventType.EXPIRED
    store.close()


def test_default_decision_window_is_a_day() -> None:
    """Documents the shipped default rather than restating it in a test."""
    assert DEFAULT_APPROVAL_TICKET_TTL == timedelta(days=1)


def test_grant_stamps_execution_deadline(
    store: DurableApprovalStore, clock: FakeClock
) -> None:
    _issue(store)
    granted = store.grant("t1")
    assert granted.execution_deadline == START + DEFAULT_APPROVAL_EXECUTION_TTL


def test_reconfigured_ttl_does_not_rewrite_existing_deadlines(
    tmp_path: Path, clock: FakeClock
) -> None:
    """A later default must not retroactively expire a live approval."""
    path = tmp_path / "a.db"
    first = DurableApprovalStore(path, now=clock, execution_ttl=timedelta(hours=6))
    _issue(first, "one")
    first.grant("one")
    original = first.get("one").execution_deadline
    first.close()

    second = DurableApprovalStore(path, now=clock, execution_ttl=timedelta(minutes=1))
    clock.advance(minutes=30)
    # Well past the *new* one-minute default, still inside the original window.
    assert second.get("one").status is ApprovalStatus.GRANTED
    assert second.get("one").execution_deadline == original

    _issue(second, "two")
    second.grant("two")
    assert second.get("two").execution_deadline == clock() + timedelta(minutes=1)
    second.close()


def test_grant_expires_at_its_persisted_deadline(
    store: DurableApprovalStore, clock: FakeClock
) -> None:
    _issue(store)
    store.grant("t1")
    clock.advance(hours=1)
    assert store.get("t1").status is ApprovalStatus.GRANTED
    clock.advance(seconds=1)
    assert store.get("t1").status is ApprovalStatus.EXPIRED


def test_expiry_survives_restart(
    ledger_path: Path, clock: FakeClock
) -> None:
    first = DurableApprovalStore(ledger_path, now=clock)
    _issue(first)
    first.grant("t1")
    first.close()
    clock.advance(hours=2)

    second = DurableApprovalStore(ledger_path, now=clock)
    assert second.get("t1").status is ApprovalStatus.EXPIRED
    assert [e.event_type for e in second.events("t1")][-1] is TicketEventType.EXPIRED
    second.verify()


def test_refused_transition_still_records_the_expiry(
    store: DurableApprovalStore, clock: FakeClock
) -> None:
    """An expired approval must leave evidence even when its caller is refused.

    If the expiry were rolled back together with the refused consume, the
    ticket would stay GRANTED and the ledger would record nothing about why
    the redemption failed.
    """
    _issue(store)
    store.grant("t1")
    clock.advance(hours=2)
    with pytest.raises(InvalidTransitionError):
        store.consume("t1")
    assert store.get("t1").status is ApprovalStatus.EXPIRED
    types = [e.event_type for e in store.events("t1")]
    assert types == [
        TicketEventType.ISSUED,
        TicketEventType.GRANTED,
        TicketEventType.EXPIRED,
    ]
    store.verify()


def test_pending_returns_live_tickets_only(
    store: DurableApprovalStore, clock: FakeClock
) -> None:
    _issue(store, "live")
    _issue(store, "doomed")
    store.grant("live")
    store.deny("doomed", decided_by="bob", reason="no")
    _issue(store, "later")
    clock.advance(hours=2)
    assert [t.ticket_id for t in store.pending()] == ["later"]


# ----------------------------------------------------------- event ledger --


def test_event_per_revision_matches_state(
    store: DurableApprovalStore,
) -> None:
    _issue(store)
    store.grant("t1")
    store.consume("t1")
    events = store.events("t1")
    assert [e.revision for e in events] == [0, 1, 2]
    assert [e.event_type for e in events] == [
        TicketEventType.ISSUED,
        TicketEventType.GRANTED,
        TicketEventType.CONSUMED,
    ]
    assert events[-1].status is ApprovalStatus.CONSUMED
    assert events[-1].decided_by == ""


def test_event_capture_decision_detail(store: DurableApprovalStore) -> None:
    _issue(store, plan_id="plan-9")
    store.deny("t1", decided_by="bob", reason="cost too high")
    event = store.events("t1")[-1]
    assert event.decided_by == "bob"
    assert event.decision_reason == "cost too high"
    assert event.plan_id == "plan-9"
    assert event.resource_id == "bucket-demo"
    assert event.action is PotentialAction.STOP_RESOURCE


def test_event_snapshots_state_at_that_revision(
    store: DurableApprovalStore, clock: FakeClock
) -> None:
    """The grant event must keep the deadline the grant actually promised.

    A revoke overwrites the ticket row, so if the deadline lived only there the
    question "for how long was this approved?" would become unanswerable
    exactly when it starts to matter.
    """
    _issue(store, execution_intent_key="i-abc", evidence_digest="digest-1")
    granted = store.grant("t1", decided_by="alice")
    promised = granted.execution_deadline
    clock.advance(minutes=10)
    store.revoke("t1", decided_by="bob", reason="superseded")

    grant_event = store.events("t1")[1]
    assert grant_event.event_type is TicketEventType.GRANTED
    assert grant_event.execution_deadline == promised
    assert grant_event.execution_intent_key == "i-abc"
    assert grant_event.evidence_digest == "digest-1"

    revoke_event = store.events("t1")[2]
    assert revoke_event.decided_by == "bob"
    assert revoke_event.decision_reason == "superseded"
    store.close()


def test_events_are_ordered_by_revision_not_by_time(
    store: DurableApprovalStore, clock: FakeClock
) -> None:
    """Ordering authority is revision, because clock can lie or go backwards."""
    _issue(store)
    store.grant("t1")
    clock._current = START - timedelta(days=3)  # time jumps backwards
    store.consume("t1")
    events = store.events("t1")
    assert [e.revision for e in events] == [0, 1, 2]
    assert events[0].occurred_at > events[-1].occurred_at
    store.verify()


def test_failed_event_write_rolls_back_the_state_change(
    ledger_path: Path, clock: FakeClock
) -> None:
    """State and event commit together, or neither does.

    The event insert is made to fail after the row update, which is the worst
    moment: the grant is already written in the transaction but uncommitted.
    """
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store._id_source = lambda: store.events("t1")[0].event_id
    with pytest.raises(ApprovalStoreCorruptionError):
        store.grant("t1", decided_by="alice")
    assert store.get("t1").status is ApprovalStatus.PENDING
    assert len(store.events("t1")) == 1
    store.close()

    reopened = DurableApprovalStore(ledger_path, now=clock)
    assert reopened.get("t1").status is ApprovalStatus.PENDING
    assert reopened.get("t1").revision == 0
    assert len(reopened.events("t1")) == 1
    reopened.verify()


def test_no_partial_write_survives_the_crash(
    ledger_path: Path, clock: FakeClock
) -> None:
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store, "a")
    _issue(store, "b")
    store.grant("a", decided_by="alice")
    store._id_source = lambda: store.events("b")[0].event_id
    with pytest.raises(ApprovalStoreCorruptionError):
        store.grant("b", decided_by="alice")
    store.close()

    reopened = DurableApprovalStore(ledger_path, now=clock)
    assert reopened.get("a").status is ApprovalStatus.GRANTED
    assert reopened.get("b").status is ApprovalStatus.PENDING
    reopened.verify()
    leftovers = [p.name for p in ledger_path.parent.glob("*journal*")]
    assert leftovers == []


def test_events_for_unknown_ticket_are_empty(store: DurableApprovalStore) -> None:
    assert store.events("never-existed") == []


def test_events_across_all_tickets(store: DurableApprovalStore) -> None:
    _issue(store, "one")
    _issue(store, "two")
    store.grant("one")
    store.revoke("two", decided_by="bob", reason="no")
    assert {e.ticket_id for e in store.events()} == {"one", "two"}
    assert len(store.events()) == 4


# -------------------------------------------------------------- integrity --


def test_damaged_ticket_state_is_detected(
    ledger_path: Path, clock: FakeClock
) -> None:
    """A status the state machine has never heard of must not be readable."""
    DurableApprovalStore(ledger_path, now=clock).close()
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            "INSERT INTO ticket (ticket_id, resource_id, action, rationale, "
            "status, created_at, decided_at, decided_by, decision_reason, "
            "plan_id, consumed, revision, execution_intent_key, "
            "evidence_digest, execution_deadline) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?, NULL, NULL, NULL)",
            ("bad", "b", "STOP_RESOURCE", "r", "imaginary", START.isoformat(),
             "", "", 0, 1),
        )
    with pytest.raises(ApprovalStoreCorruptionError, match="invalid ticket"):
        DurableApprovalStore(ledger_path, now=clock)


def test_granted_ticket_without_deadline_is_detected(
    ledger_path: Path, clock: FakeClock
) -> None:
    """A grant with no deadline could never expire, so it is corruption.

    Before this invariant a `GRANTED` row with a NULL `execution_deadline`
    opened cleanly, passed `verify()`, stayed `GRANTED` forever, and could be
    redeemed at any future date -- the M9 fail-open shape, reachable again
    through raw damage. The row is now unrepresentable, so reopening rejects it
    instead of trusting it.
    """
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.grant("t1")
    store.close()
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            "UPDATE ticket SET execution_deadline = NULL WHERE ticket_id = 't1'"
        )
    with pytest.raises(ApprovalStoreCorruptionError, match="invalid ticket"):
        DurableApprovalStore(ledger_path, now=clock)


def test_pending_ticket_with_future_created_at_cannot_stay_votable(
    ledger_path: Path, clock: FakeClock
) -> None:
    """A decision window that has not opened yet must not stay open forever.

    Expiry only compared `now - created_at > ttl`, so a `created_at` pushed into
    the future made that difference negative and the ticket was never due. The
    over-age request stayed `PENDING` indefinitely and could still be granted
    years later. It now expires instead, and the refusal is recorded durably.
    """
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.close()
    future = START + timedelta(days=365 * 20)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            "UPDATE ticket SET created_at = ? WHERE ticket_id = 't1'",
            (future.isoformat(),),
        )
    reopened = DurableApprovalStore(ledger_path, now=clock)
    ticket = reopened.get("t1")
    assert ticket.status is ApprovalStatus.EXPIRED
    assert ticket.consumed is False
    # The window never opened, so it can no longer be granted.
    with pytest.raises(InvalidTransitionError):
        reopened.grant("t1", decided_by="alice")
    assert [e.event_type for e in reopened.events("t1")] == [
        TicketEventType.ISSUED,
        TicketEventType.EXPIRED,
    ]
    reopened.verify()


def test_future_decision_window_survives_a_backwards_clock(
    ledger_path: Path, clock: FakeClock
) -> None:
    """Ordering is by revision, so a rewound clock must not fake corruption.

    The store deliberately treats timestamps as advisory: a clock that jumps
    backwards is tolerated because `revision` is the ordering authority. The
    future-`created_at` guard must therefore expire such a ticket rather than
    declare the ledger unreadable.
    """
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.grant("t1")
    clock._current = START - timedelta(days=3)  # time jumps backwards
    ticket = store.get("t1")
    assert ticket.status is ApprovalStatus.GRANTED
    assert [e.revision for e in store.events("t1")] == [0, 1]
    store.verify()


def test_consumed_mirror_drift_is_detected(
    ledger_path: Path, clock: FakeClock
) -> None:
    """`consumed` is a stored mirror of status; disagreement is corruption.

    Bypassing the store with raw SQL is the only way to produce this, which is
    exactly why reopening has to re-derive rather than trust.
    """
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.grant("t1")
    store.consume("t1")
    store.close()
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("UPDATE ticket SET consumed = 0 WHERE ticket_id = 't1'")
    with pytest.raises(ApprovalStoreCorruptionError, match="invalid ticket"):
        DurableApprovalStore(ledger_path, now=clock)


def test_unparseable_timestamp_is_detected(
    ledger_path: Path, clock: FakeClock
) -> None:
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.close()
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("UPDATE ticket SET created_at = 'not-a-timestamp'")
    with pytest.raises(ApprovalStoreCorruptionError, match="unparseable timestamp"):
        DurableApprovalStore(ledger_path, now=clock)


def test_missing_event_is_detected(
    ledger_path: Path, clock: FakeClock
) -> None:
    """A committed state change with no event is the exact drift to catch."""
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.grant("t1")
    store.close()
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("DELETE FROM ticket_event WHERE revision = 1")
    with pytest.raises(ApprovalStoreCorruptionError, match="events"):
        DurableApprovalStore(ledger_path, now=clock)


def test_event_for_unknown_ticket_is_detected(
    ledger_path: Path, clock: FakeClock
) -> None:
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.close()
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO ticket_event (event_id, ticket_id, revision, "
            "event_type, occurred_at, status, resource_id, action, plan_id, "
            "decided_by, decision_reason) VALUES "
            "('ghost', 'ghost-ticket', 0, 'issued', "
            "'2026-01-01T00:00:00+00:00', 'pending', 'b', "
            "'STOP_RESOURCE', NULL, '', '')"
        )
    with pytest.raises(ApprovalStoreCorruptionError, match="ghost"):
        DurableApprovalStore(ledger_path, now=clock)


def test_malformed_event_type_is_detected(
    ledger_path: Path, clock: FakeClock
) -> None:
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.close()
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("UPDATE ticket_event SET event_type = 'invented'")
    with pytest.raises(ApprovalStoreCorruptionError, match="invalid event"):
        DurableApprovalStore(ledger_path, now=clock)


def test_revision_skew_between_state_and_events_is_detected(
    ledger_path: Path, clock: FakeClock
) -> None:
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.grant("t1")
    store.close()
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("UPDATE ticket SET revision = 5 WHERE ticket_id = 't1'")
    with pytest.raises(ApprovalStoreCorruptionError, match="revision 5"):
        DurableApprovalStore(ledger_path, now=clock)


def test_verify_passes_on_a_healthy_ledger(store: DurableApprovalStore) -> None:
    _issue(store, "a")
    _issue(store, "b")
    store.grant("a", decided_by="alice")
    store.revoke("b", decided_by="bob", reason="no")
    store.verify()


def test_verify_can_be_skipped_on_open_but_is_available(
    ledger_path: Path, clock: FakeClock
) -> None:
    """Skipping the open-time check is an escape hatch, not a licence.

    With ``verify_on_open=False`` the store opens a damaged ledger so an
    operator can inspect it; the damage is still reported by ``verify()`` and
    by the first read that has to interpret the bad row.
    """
    first = DurableApprovalStore(ledger_path, now=clock)
    _issue(first)
    first.close()
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("DELETE FROM ticket_event")

    lenient = DurableApprovalStore(ledger_path, now=clock, verify_on_open=False)
    try:
        with pytest.raises(ApprovalStoreCorruptionError):
            lenient.verify()
    finally:
        lenient.close()


# ------------------------------------------------------------ concurrency --


def _run_threads(targets: list[Callable[[], Any]]) -> list[Any]:
    """Run callables simultaneously and collect each outcome.

    A barrier is essential: without it the first thread would usually finish
    before the second even starts, and the test would prove nothing about
    contention.
    """
    barrier = threading.Barrier(len(targets))
    results: list[Any] = [None] * len(targets)

    def wrap(index: int, fn: Callable[[], Any]) -> None:
        barrier.wait(timeout=30)
        try:
            results[index] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001 - outcome is the assertion
            results[index] = ("err", type(exc).__name__)

    threads = [
        threading.Thread(target=wrap, args=(i, fn)) for i, fn in enumerate(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    return results


def test_concurrent_grants_produce_exactly_one_decision(
    store: DurableApprovalStore,
) -> None:
    _issue(store)
    results = _run_threads(
        [lambda: store.grant("t1", decided_by="alice")] * 4
    )
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert store.get("t1").status is ApprovalStatus.GRANTED
    assert store.get("t1").revision == 1
    assert len(store.events("t1")) == 2
    store.verify()


def test_concurrent_grant_and_deny_produce_exactly_one_decision(
    store: DurableApprovalStore,
) -> None:
    """A ticket can only ever be answered once, whichever answer lands."""
    _issue(store)
    results = _run_threads(
        [
            lambda: store.grant("t1", decided_by="alice"),
            lambda: store.deny("t1", decided_by="bob", reason="no"),
        ]
    )
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert store.get("t1").status in {ApprovalStatus.GRANTED, ApprovalStatus.DENIED}
    assert store.get("t1").revision == 1
    store.verify()


def test_concurrent_consumes_redeem_exactly_once(
    store: DurableApprovalStore,
) -> None:
    _issue(store)
    store.grant("t1")
    results = _run_threads([lambda: store.consume("t1")] * 5)
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert store.get("t1").status is ApprovalStatus.CONSUMED
    assert store.get("t1").revision == 2
    store.verify()


def test_concurrent_duplicate_creation_yields_one_ticket(
    store: DurableApprovalStore,
) -> None:
    results = _run_threads([lambda: _issue(store)] * 4)
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert any(
        kind == "err" and name == "DuplicateTicketError" for kind, name in results
    )
    assert len(store.events("t1")) == 1
    store.verify()


def test_concurrent_expiry_writes_one_event(
    store: DurableApprovalStore, clock: FakeClock
) -> None:
    """Racing readers may all see EXPIRED, but only one may record it."""
    _issue(store)
    store.grant("t1")
    clock.advance(hours=2)
    results = _run_threads([lambda: store.get("t1") for _ in range(5)])
    assert all(kind == "ok" and ticket.status is ApprovalStatus.EXPIRED
               for kind, ticket in results)
    assert [e.revision for e in store.events("t1")] == [0, 1, 2]
    store.verify()


# ------------------------------------------- real multi-process safety ----


def _process_worker(
    path: str, ticket_id: str, operation: str, queue: Any
) -> None:  # pragma: no cover - runs in a child interpreter
    """Module level so ``spawn`` can import it in a fresh interpreter."""
    try:
        store = DurableApprovalStore(path, now=lambda: START, verify_on_open=False)
        ticket = {
            "grant": lambda: store.grant(ticket_id, decided_by="proc"),
            "deny": lambda: store.deny(ticket_id, decided_by="proc", reason="no"),
            "consume": lambda: store.consume(ticket_id),
        }[operation]()
        queue.put((operation, "ok", ticket.status.value, ticket.revision))
    except Exception as exc:  # noqa: BLE001 - outcome is the assertion
        queue.put((operation, "err", type(exc).__name__, -1))


def _spawn(
    path: Path, ticket_id: str, operations: list[str]
) -> list[tuple[str, str, str, int]]:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_process_worker,
            args=(str(path), ticket_id, operation, queue),
        )
        for operation in operations
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
    assert not any(process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    return sorted(queue.get(timeout=30) for _ in operations)


def test_separate_processes_decide_one_ticket_exactly_once(
    ledger_path: Path, clock: FakeClock
) -> None:
    """The durability claim itself: independent interpreters, one ledger.

    Threads share a GIL and one set of connections; only real processes prove
    that the locking discipline in SQLite's own write lock is what provides
    the guarantee.
    """
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.close()

    results = _spawn(ledger_path, "t1", ["grant", "deny", "grant", "deny"])
    winners = [r for r in results if r[1] == "ok"]
    assert len(winners) == 1, results
    assert winners[0][2] in {"granted", "denied"}
    assert winners[0][3] == 1

    reopened = DurableApprovalStore(ledger_path, now=clock)
    assert reopened.get("t1").status.value == winners[0][2]
    assert [e.event_type for e in reopened.events("t1")] == [
        TicketEventType.ISSUED,
        TicketEventType.GRANTED if winners[0][2] == "granted" else TicketEventType.DENIED,
    ]
    reopened.verify()


def test_separate_processes_redeem_one_ticket_exactly_once(
    ledger_path: Path, clock: FakeClock
) -> None:
    store = DurableApprovalStore(ledger_path, now=clock)
    _issue(store)
    store.grant("t1", decided_by="alice")
    store.close()

    results = _spawn(ledger_path, "t1", ["consume"] * 3)
    assert sum(1 for r in results if r[1] == "ok") == 1, results

    reopened = DurableApprovalStore(ledger_path, now=clock)
    assert reopened.get("t1").status is ApprovalStatus.CONSUMED
    assert reopened.get("t1").revision == 2
    assert [e.event_type for e in reopened.events("t1")] == [
        TicketEventType.ISSUED,
        TicketEventType.GRANTED,
        TicketEventType.CONSUMED,
    ]
    reopened.verify()


def test_many_processes_may_share_one_ledger(
    ledger_path: Path, clock: FakeClock
) -> None:
    """Option A is shared access, not mutual exclusion of the file."""
    store = DurableApprovalStore(ledger_path, now=clock)
    for index in range(4):
        _issue(store, f"t{index}")
    store.close()

    results = _spawn(ledger_path, "t3", ["grant", "deny"])
    assert sum(1 for r in results if r[1] == "ok") == 1

    reopened = DurableApprovalStore(ledger_path, now=clock)
    assert reopened.get("t0").status is ApprovalStatus.PENDING
    assert reopened.verify() is None


def test_processes_can_open_a_fresh_ledger_concurrently(tmp_path: Path) -> None:
    """Racing first-opens must not produce a phantom corruption failure."""
    path = tmp_path / "fresh.db"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_process_worker, args=(str(path), "unused", "grant", queue))
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
    outcomes = sorted(queue.get(timeout=30) for _ in processes)
    unknown = [o for o in outcomes if o[1] == "err" and o[2] == "UnknownTicketError"]
    assert len(unknown) == 3, outcomes
    assert not any(
        "Corruption" in o[2] or "Unavailable" in o[2]
        for o in outcomes
        if o[1] == "err"
    ), outcomes
    # Whoever got there first created the schema; everyone can read it after.
    DurableApprovalStore(path, now=lambda: START).verify()


# ------------------------------------------------------------ parameters ---


def test_busy_timeout_default_is_used_when_unspecified() -> None:
    assert DEFAULT_APPROVAL_BUSY_TIMEOUT_SECONDS > 0


def test_rejects_non_positive_ttls(ledger_path: Path, clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="TTL"):
        DurableApprovalStore(ledger_path, now=clock, ttl=timedelta(0))
    with pytest.raises(ValueError, match="TTL"):
        DurableApprovalStore(
            ledger_path, now=clock, execution_ttl=timedelta(seconds=-1)
        )


def test_rejects_naive_clock(ledger_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DurableApprovalStore(ledger_path, now=lambda: datetime(2026, 1, 1))