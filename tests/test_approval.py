"""Approval ticket store: state machine, TTL expiration, and boundaries.

These tests are hermetic: no network, no AWS, and no wall-clock sleeps.
Time is injected through a fixed clock so expiration is deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sws_agent.approval import (
    DEFAULT_APPROVAL_EXECUTION_TTL,
    DEFAULT_APPROVAL_TICKET_TTL,
    LEGAL_APPROVAL_TRANSITIONS,
    TERMINAL_APPROVAL_STATUSES,
    DuplicateTicketError,
    InMemoryApprovalStore,
    InvalidTransitionError,
    UnknownTicketError,
)
from sws_agent.constants import ApprovalStatus, PotentialAction
from sws_agent.interfaces import ApprovalStore
from sws_agent.models import ApprovalTicket

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeClock:
    """Deterministic, mutable time source for approval tests."""

    def __init__(self, current: datetime = START) -> None:
        self._current = current

    def now(self) -> datetime:
        return self._current

    def advance(self, **delta) -> None:
        self._current = self._current + timedelta(**delta)


def _store(ttl: timedelta = timedelta(minutes=60)):
    clock = FakeClock()
    return clock, InMemoryApprovalStore(now=clock.now, ttl=ttl)


def test_create_ticket_defaults_to_pending():
    clock, store = _store()
    ticket = store.create_ticket(
        "bucket-example", PotentialAction.STOP_RESOURCE, "demo run"
    )
    assert isinstance(ticket, ApprovalTicket)
    assert ticket.status is ApprovalStatus.PENDING
    assert ticket.ticket_id
    assert ticket.created_at == START
    assert ticket.decided_at is None
    assert ticket.decided_by == ""
    assert ticket.decision_reason == ""
    assert ticket.rationale == "demo run"


def test_create_ticket_honors_explicit_ticket_id():
    _, store = _store()
    ticket = store.create_ticket(
        "bucket-example",
        PotentialAction.STOP_RESOURCE,
        ticket_id="ticket-1",
    )
    assert ticket.ticket_id == "ticket-1"


def test_pending_returns_created_tickets_in_order():
    _, store = _store()
    store.create_ticket("r1", PotentialAction.LEAVE, ticket_id="t1")
    store.create_ticket("r2", PotentialAction.REQUEST_APPROVAL, ticket_id="t2")
    store.create_ticket("r3", PotentialAction.STOP_RESOURCE, ticket_id="t3")
    assert [t.ticket_id for t in store.pending()] == ["t1", "t2", "t3"]


def test_grant_records_decision_and_leaves_pending():
    clock, store = _store()
    store.create_ticket(
        "bucket-example", PotentialAction.STOP_RESOURCE, ticket_id="t1"
    )
    clock.advance(minutes=5)
    granted = store.grant("t1", decided_by="alice", reason="approved manually")
    assert granted.status is ApprovalStatus.GRANTED
    assert granted.decided_at == clock.now()
    assert granted.decided_by == "alice"
    assert granted.decision_reason == "approved manually"
    assert store.pending() == []


def test_deny_records_decision():
    clock, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    clock.advance(seconds=30)
    denied = store.deny("t1", decided_by="bob", reason="not now")
    assert denied.status is ApprovalStatus.DENIED
    assert denied.decided_at == clock.now()
    assert denied.decided_by == "bob"
    assert denied.decision_reason == "not now"
    assert store.pending() == []


def test_explicit_expire_transition():
    clock, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    clock.advance(seconds=30)
    expired = store.expire("t1", reason="manual expiry")
    assert expired.status is ApprovalStatus.EXPIRED
    assert expired.decided_at == clock.now()
    assert store.pending() == []


def test_transition_after_grant_raises():
    """M12: DENIED and REVOKED are still unreachable from GRANTED.

    ``expire`` is deliberately *not* in this list any more: M12 makes
    GRANTED -> EXPIRED legal so a grant can be bounded by its execution
    deadline. See ``test_grant_then_expire_is_legal``.
    """
    _, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    with pytest.raises(InvalidTransitionError):
        store.deny("t1")


def test_transition_after_revoke_raises():
    _, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.revoke("t1")
    for call in (store.grant, store.deny, store.expire, store.consume):
        with pytest.raises(InvalidTransitionError):
            call("t1")


def test_transition_after_consume_raises():
    _, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    store.consume("t1")
    for call in (store.grant, store.deny, store.expire, store.revoke, store.consume):
        with pytest.raises(InvalidTransitionError):
            call("t1")


def test_transition_after_deny_raises():
    _, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.deny("t1")
    with pytest.raises(InvalidTransitionError):
        store.grant("t1")


def test_transition_after_explicit_expire_raises():
    _, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.expire("t1")
    with pytest.raises(InvalidTransitionError):
        store.grant("t1")


def test_get_unknown_ticket_raises():
    _, store = _store()
    with pytest.raises(UnknownTicketError):
        store.get("missing")
    with pytest.raises(UnknownTicketError):
        store.grant("missing")


def test_get_auto_expires_stale_pending_ticket():
    clock, store = _store(ttl=timedelta(hours=1))
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    clock.advance(hours=2)
    expired = store.get("t1")
    assert expired.status is ApprovalStatus.EXPIRED
    assert "expired" in expired.decision_reason.lower()
    assert store.pending() == []


def test_grant_after_ttl_expiry_raises():
    clock, store = _store(ttl=timedelta(hours=1))
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    clock.advance(hours=1, seconds=1)
    with pytest.raises(InvalidTransitionError):
        store.grant("t1")


def test_pending_excludes_tickets_exactly_at_ttl_boundary():
    clock, store = _store(ttl=timedelta(hours=1))
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    clock.advance(hours=1)
    assert [t.ticket_id for t in store.pending()] == ["t1"]


def test_default_ttl_is_defined():
    assert DEFAULT_APPROVAL_TICKET_TTL > timedelta(0)


# ---------------------------------------------------------------------------
# M12: explicit transition table, monotonic revision, revocation, and the
# execution deadline that bounds a grant.
# ---------------------------------------------------------------------------


def _store2(
    ttl: timedelta = timedelta(hours=24),
    execution_ttl: timedelta = timedelta(hours=1),
):
    clock = FakeClock()
    store = InMemoryApprovalStore(
        now=clock.now, ttl=ttl, execution_ttl=execution_ttl
    )
    return clock, store


def test_new_ticket_starts_pending_at_revision_zero():
    _, store = _store2()
    ticket = store.create_ticket(
        "r1", PotentialAction.STOP_RESOURCE, ticket_id="t1"
    )
    assert ticket.status is ApprovalStatus.PENDING
    assert ticket.revision == 0
    assert ticket.consumed is False
    assert ticket.decided_at is None
    assert ticket.execution_deadline is None
    assert ticket.execution_intent_key is None
    assert ticket.evidence_digest is None


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("grant", ApprovalStatus.GRANTED),
        ("deny", ApprovalStatus.DENIED),
        ("expire", ApprovalStatus.EXPIRED),
        ("revoke", ApprovalStatus.REVOKED),
    ],
)
def test_every_legal_transition_from_pending(method, expected):
    _, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    moved = getattr(store, method)("t1")
    assert moved.status is expected
    assert moved.revision == 1


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("consume", ApprovalStatus.CONSUMED),
        ("revoke", ApprovalStatus.REVOKED),
        ("expire", ApprovalStatus.EXPIRED),
    ],
)
def test_every_legal_transition_from_granted(method, expected):
    _, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    moved = getattr(store, method)("t1")
    assert moved.status is expected
    assert moved.revision == 2


def test_grant_and_deny_are_unreachable_from_granted():
    """M12: a decided ticket cannot be re-decided in either direction."""
    _, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    for call in (store.grant, store.deny):
        with pytest.raises(InvalidTransitionError):
            call("t1")


@pytest.mark.parametrize(
    "setup", ["denied", "explicitly_expired", "consumed", "revoked"]
)
def test_terminal_states_admit_no_further_transition(setup):
    """M12: DENIED, EXPIRED, CONSUMED, and REVOKED are all terminal."""
    _, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    if setup == "denied":
        store.deny("t1")
    elif setup == "explicitly_expired":
        store.expire("t1")
    elif setup == "consumed":
        store.grant("t1")
        store.consume("t1")
    elif setup == "revoked":
        store.revoke("t1")
    before = store.get("t1")
    for call in (
        store.grant,
        store.deny,
        store.expire,
        store.revoke,
        store.consume,
    ):
        with pytest.raises(InvalidTransitionError):
            call("t1")
    after = store.get("t1")
    assert after.status is before.status
    assert after.revision == before.revision


def test_terminal_status_table_is_exhaustively_empty():
    for status in TERMINAL_APPROVAL_STATUSES:
        assert LEGAL_APPROVAL_TRANSITIONS[status] == frozenset()
    assert LEGAL_APPROVAL_TRANSITIONS[ApprovalStatus.PENDING] == frozenset(
        {
            ApprovalStatus.GRANTED,
            ApprovalStatus.DENIED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.REVOKED,
        }
    )
    assert LEGAL_APPROVAL_TRANSITIONS[ApprovalStatus.GRANTED] == frozenset(
        {
            ApprovalStatus.CONSUMED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.REVOKED,
        }
    )
    assert TERMINAL_APPROVAL_STATUSES == frozenset(
        {
            ApprovalStatus.DENIED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.CONSUMED,
            ApprovalStatus.REVOKED,
        }
    )


def test_second_consume_is_refused():
    _, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    store.consume("t1")
    with pytest.raises(InvalidTransitionError):
        store.consume("t1")


def test_second_decision_is_refused():
    clock, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1", decided_by="alice")
    clock.advance(seconds=1)
    with pytest.raises(InvalidTransitionError):
        store.deny("t1", decided_by="bob")
    assert store.get("t1").status is ApprovalStatus.GRANTED
    assert store.get("t1").decided_by == "alice"


def test_consume_mirrors_status():
    _, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    assert store.get("t1").consumed is False
    consumed = store.consume("t1")
    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.consumed is True
    assert store.get("t1").status is ApprovalStatus.CONSUMED


def test_revoked_ticket_leaves_pending_queue():
    _, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    revoked = store.revoke("t1", decided_by="alice", reason="superseded")
    assert revoked.status is ApprovalStatus.REVOKED
    assert revoked.decided_by == "alice"
    assert revoked.decision_reason == "superseded"
    assert store.pending() == []


def test_revoked_ticket_is_absent_from_pending():
    _, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.create_ticket("r2", PotentialAction.STOP_RESOURCE, ticket_id="t2")
    store.revoke("t1")
    assert [t.ticket_id for t in store.pending()] == ["t2"]


def test_revision_advances_once_per_committed_transition():
    clock, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    assert store.get("t1").revision == 0
    store.grant("t1")
    assert store.get("t1").revision == 1
    clock.advance(seconds=1)
    store.revoke("t1")
    assert store.get("t1").revision == 2
    clock.advance(seconds=1)
    with pytest.raises(InvalidTransitionError):
        store.consume("t1")
    assert store.get("t1").revision == 2


def test_grant_stamps_execution_deadline():
    clock, store = _store2(execution_ttl=timedelta(minutes=30))
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    clock.advance(minutes=10)
    granted = store.grant("t1")
    assert granted.execution_deadline == clock.now() + timedelta(minutes=30)
    assert granted.decided_at == clock.now()


def test_grant_survives_inside_execution_window():
    """M12: the fix for the M9 defect where a GRANTED ticket never expired."""
    clock, store = _store2(
        ttl=timedelta(minutes=1), execution_ttl=timedelta(hours=1)
    )
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    clock.advance(minutes=30)
    assert store.get("t1").status is ApprovalStatus.GRANTED
    assert store.consume("t1").status is ApprovalStatus.CONSUMED


def test_grant_expires_after_execution_deadline():
    """M12: a grant is bounded by the execution deadline, not the decision TTL."""
    clock, store = _store2(
        ttl=timedelta(minutes=1), execution_ttl=timedelta(minutes=15)
    )
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    clock.advance(minutes=16)
    expired = store.get("t1")
    assert expired.status is ApprovalStatus.EXPIRED
    assert "execution window" in expired.decision_reason
    assert expired.revision == 2


def test_grant_is_not_expired_exactly_at_the_deadline():
    clock, store = _store2(execution_ttl=timedelta(minutes=15))
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    clock.advance(minutes=15)
    assert store.get("t1").status is ApprovalStatus.GRANTED


def test_expired_grant_cannot_be_consumed():
    clock, store = _store2(execution_ttl=timedelta(minutes=5))
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    clock.advance(minutes=6)
    with pytest.raises(InvalidTransitionError):
        store.consume("t1")


def test_decision_ttl_and_execution_ttl_are_independent():
    """A long decision window must not extend the execution window."""
    clock, store = _store2(
        ttl=timedelta(days=7), execution_ttl=timedelta(minutes=5)
    )
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    clock.advance(days=6)
    assert store.get("t1").status is ApprovalStatus.PENDING
    store.grant("t1")
    clock.advance(minutes=6)
    assert store.get("t1").status is ApprovalStatus.EXPIRED


def test_execution_ttl_default_is_documented_as_a_placeholder():
    """The grant window is an M12 implementation default, not a product rule.

    M9 left a grant valid forever, so some bound had to be chosen. The value
    is injected rather than hard-coded at each call site, and the caveat is
    asserted against the module source because a bare string after an
    assignment is not a runtime ``__doc__`` on the object.
    """
    from pathlib import Path

    assert DEFAULT_APPROVAL_EXECUTION_TTL == timedelta(hours=1)
    source = (
        Path(__file__).parent.parent / "src/sws_agent/approval.py"
    ).read_text(encoding="utf-8")
    assert "not a product decision" in source
    assert "implementation default" in source


def test_intent_key_and_evidence_digest_are_recorded_verbatim():
    """Phase 1 records caller-supplied values without deriving them."""
    _, store = _store2()
    ticket = store.create_ticket(
        "r1",
        PotentialAction.STOP_RESOURCE,
        ticket_id="t1",
        execution_intent_key="abc123",
        evidence_digest="def456",
    )
    assert ticket.execution_intent_key == "abc123"
    assert ticket.evidence_digest == "def456"


def test_in_memory_store_satisfies_the_approval_store_protocol():
    assert isinstance(_store2()[1], ApprovalStore)


def test_consume_preserves_the_humans_decision_time_and_reason():
    """Redemption must not overwrite the record of why the human said yes."""
    clock, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    clock.advance(minutes=2)
    granted = store.grant("t1", decided_by="alice", reason="change window")
    granted_at = granted.decided_at
    clock.advance(minutes=3)
    consumed = store.consume("t1")
    assert consumed.decided_at == granted_at
    assert consumed.decided_by == "alice"
    assert consumed.decision_reason == "change window"
    assert consumed.revision == 2


def test_revoking_a_grant_preserves_the_original_decision_time():
    clock, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    clock.advance(minutes=2)
    store.grant("t1", decided_by="alice", reason="ok")
    granted_at = store.get("t1").decided_at
    clock.advance(minutes=4)
    revoked = store.revoke("t1", decided_by="bob", reason="superseded")
    assert revoked.decided_at == granted_at
    assert revoked.decided_by == "bob"
    assert revoked.decision_reason == "superseded"


def test_first_transition_stamps_decided_at():
    clock, store = _store2()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    assert store.get("t1").decided_at is None
    clock.advance(minutes=1)
    denied = store.deny("t1", decided_by="bob")
    assert denied.decided_at == clock.now()


def test_consumed_flag_cannot_contradict_status():
    base = {
        "ticket_id": "t1",
        "resource_id": "r1",
        "action": PotentialAction.STOP_RESOURCE,
        "created_at": START,
    }
    with pytest.raises(ValueError, match="must mirror status"):
        ApprovalTicket(**base, status=ApprovalStatus.CONSUMED, consumed=False)
    with pytest.raises(ValueError, match="must mirror status"):
        ApprovalTicket(**base, status=ApprovalStatus.GRANTED, consumed=True)
    assert ApprovalTicket(
        **base, status=ApprovalStatus.CONSUMED, consumed=True
    ).status is ApprovalStatus.CONSUMED


# --- M12 Phase 3A: duplicate-ticket parity with DurableApprovalStore ---


def test_duplicate_ticket_id_is_refused_and_leaves_granted_ticket_intact():
    """A duplicate create must not be able to un-approve a GRANTED ticket.

    This store used to assign unconditionally, so re-creating a ticket_id
    reset it to PENDING at revision 0 -- a live approval could be silently
    revoked (or, in the other direction, a denial erased). It now raises
    DuplicateTicketError and leaves the stored ticket exactly as it was.
    """
    clock, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, "run A", ticket_id="dup")
    granted = store.grant("dup", decided_by="alice")
    assert granted.status is ApprovalStatus.GRANTED

    with pytest.raises(DuplicateTicketError, match="dup"):
        store.create_ticket(
            "r2",
            PotentialAction.STOP_RESOURCE,
            "attacker run",
            ticket_id="dup",
        )

    # The original ticket is untouched: still GRANTED, same revision, same
    # decision fields, and no trace of the rejected create.
    after = store.get("dup")
    assert after == granted
    assert after.status is ApprovalStatus.GRANTED
    assert after.revision == granted.revision
    assert after.decided_by == "alice"
    assert after.decided_at == granted.decided_at
    assert after.resource_id == "r1"
    assert after.rationale == "run A"
    # And the rejected ticket's distinct fields leaked nowhere.
    assert "attacker run" not in {t.rationale for t in store.pending()}
    assert store.pending() == []


def test_duplicate_refusal_is_independent_of_stored_status():
    """Duplicate detection keys on identity, not on the stored status."""
    clock, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="d1")
    store.grant("d1", decided_by="alice")
    store.create_ticket("r2", PotentialAction.STOP_RESOURCE, ticket_id="d2")
    store.deny("d2", decided_by="bob")

    for ticket_id in ("d1", "d2"):
        with pytest.raises(DuplicateTicketError):
            store.create_ticket(
                "r3", PotentialAction.STOP_RESOURCE, ticket_id=ticket_id
            )
    assert store.get("d1").status is ApprovalStatus.GRANTED
    assert store.get("d2").status is ApprovalStatus.DENIED


def test_refused_duplicate_does_not_advance_revision_counter():
    """A rejected create must not consume a revision number or an id slot."""
    clock, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="x")
    granted = store.grant("x", decided_by="alice")

    for _ in range(3):
        with pytest.raises(DuplicateTicketError):
            store.create_ticket(
                "r1", PotentialAction.STOP_RESOURCE, ticket_id="x"
            )

    assert store.get("x") == granted
    # A subsequent legitimate create still works and starts at revision 0.
    fresh = store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="y")
    assert fresh.revision == 0


def test_distinct_ticket_ids_and_autogen_ids_still_create_normally():
    """The guard rejects only true duplicates; normal creation is unchanged."""
    clock, store = _store()
    first = store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    second = store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t2")
    auto_a = store.create_ticket("r1", PotentialAction.STOP_RESOURCE)
    auto_b = store.create_ticket("r1", PotentialAction.STOP_RESOURCE)

    ids = {first.ticket_id, second.ticket_id, auto_a.ticket_id, auto_b.ticket_id}
    assert len(ids) == 4  # uuid4 path still produces distinct ids
    for ticket in (first, second, auto_a, auto_b):
        assert ticket.status is ApprovalStatus.PENDING
        assert ticket.revision == 0
    assert len(store.pending()) == 4
