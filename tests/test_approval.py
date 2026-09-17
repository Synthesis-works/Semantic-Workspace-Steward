"""Approval ticket store: state machine, TTL expiration, and boundaries.

These tests are hermetic: no network, no AWS, and no wall-clock sleeps.
Time is injected through a fixed clock so expiration is deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sws_agent.approval import (
    DEFAULT_APPROVAL_TICKET_TTL,
    InMemoryApprovalStore,
    InvalidTransitionError,
    UnknownTicketError,
)
from sws_agent.constants import ApprovalStatus, PotentialAction
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
    _, store = _store()
    store.create_ticket("r1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    store.grant("t1")
    with pytest.raises(InvalidTransitionError):
        store.deny("t1")
    with pytest.raises(InvalidTransitionError):
        store.expire("t1")


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