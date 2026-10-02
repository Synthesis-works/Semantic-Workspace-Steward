"""M13 Phase 3A: exact-revision parity at the approval redemption boundary.

M13's execution ledger binds a reservation to the exact approval revision it
was claimed against. Before this module existed, that binding could not be
enforced where it matters: ``DurableApprovalStore.consume`` accepted
``expected_revision``, but the ``ApprovalStore`` protocol declared only
``consume(ticket_id)`` and ``InMemoryApprovalStore`` had no revision
precondition at all.

A caller coding against the protocol therefore could not redeem the revision it
had verified. It could reserve against revision *R*, let the ticket advance, and
redeem the newer revision while the execution ledger still asserted *R* -- the
two authorities would disagree about which authorization was used, which is the
gap this contract closes.

These tests assert parity between the two implementations. Deliberately absent:
any wiring between the two stores. The ledger records a revision, approval
answers whether a ticket is at that revision, and reconciling the two is the
coordinator's job in Phase 4 -- a responsibility this phase deliberately does not
take. There is no AWS here, and no execution.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sws_agent.approval import (
    InMemoryApprovalStore,
    InvalidTransitionError,
    RevisionConflictError,
    UnknownTicketError,
)
from sws_agent.approval_ledger import DurableApprovalStore
from sws_agent.constants import ApprovalStatus, PotentialAction
from sws_agent.execution_ledger import DurableExecutionLedger
from sws_agent.interfaces import ApprovalStore

START = datetime(2026, 1, 1, tzinfo=timezone.utc)

TICKET = "t1"
INTENT = "intent-aaa"


class FakeClock:
    """Deterministic, mutable time source, mirroring the other approval tests."""

    def __init__(self, current: datetime = START) -> None:
        self._current = current

    def __call__(self) -> datetime:
        return self._current


def _grant(store, ticket_id: str = TICKET):
    """Issue then grant, returning the granted ticket at revision 1."""
    store.create_ticket(
        resource_id="i-1",
        action=PotentialAction.STOP_RESOURCE,
        ticket_id=ticket_id,
    )
    return store.grant(ticket_id)


def _memory() -> InMemoryApprovalStore:
    return InMemoryApprovalStore()


def _durable(ledger_path: Path, clock: FakeClock) -> DurableApprovalStore:
    return DurableApprovalStore(ledger_path, now=clock)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "approval.sqlite3"


# --- protocol parity ---------------------------------------------------------


def test_both_stores_satisfy_the_protocol() -> None:
    store = _memory()
    assert isinstance(store, ApprovalStore)
    assert isinstance(DurableApprovalStore, type)


def test_consume_accepts_expected_revision_on_every_implementation(
    ledger_path: Path, clock: FakeClock
) -> None:
    """The protocol's keyword must exist on the concrete classes.

    A ``Protocol`` is ``runtime_checkable`` on method *names* only, so it
    cannot catch a signature that has drifted behind the declared contract.
    This asserts the parameter is actually accepted.
    """
    import inspect

    for store in (_memory(), _durable(ledger_path, clock)):
        assert isinstance(store, ApprovalStore)
        parameter = inspect.signature(store.consume).parameters
        assert "expected_revision" in parameter, type(store).__name__
        assert parameter["expected_revision"].default is None
        assert parameter["expected_revision"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter["expected_revision"].annotation in (
            "int | None",
            int | None,
        )


# --- matching revision succeeds, both stores --------------------------------


def test_matching_revision_consumes_in_memory() -> None:
    store = _memory()
    granted = _grant(store)
    consumed = store.consume(TICKET, expected_revision=granted.revision)
    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.revision == granted.revision + 1
    assert consumed.consumed is True


def test_matching_revision_consumes_durably(
    ledger_path: Path, clock: FakeClock
) -> None:
    store = _durable(ledger_path, clock)
    granted = _grant(store)
    consumed = store.consume(TICKET, expected_revision=granted.revision)
    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.revision == granted.revision + 1
    store.verify()
    store.close()

    reopened = DurableApprovalStore(ledger_path, now=clock)
    try:
        assert reopened.get(TICKET).status is ApprovalStatus.CONSUMED
        reopened.verify()
    finally:
        reopened.close()


# --- stale revision refuses, both stores ------------------------------------


def test_stale_revision_refuses_in_memory() -> None:
    store = _memory()
    issued = store.create_ticket(
        resource_id="i-1",
        action=PotentialAction.STOP_RESOURCE,
        ticket_id=TICKET,
    )
    granted = store.grant(TICKET)
    assert granted.revision == issued.revision + 1
    with pytest.raises(RevisionConflictError):
        store.consume(TICKET, expected_revision=issued.revision)
    # Refused means untouched: the newer approval survives.
    survivor = store.get(TICKET)
    assert survivor.status is ApprovalStatus.GRANTED
    assert survivor.revision == granted.revision


def test_stale_revision_refuses_durably(ledger_path: Path, clock: FakeClock) -> None:
    store = _durable(ledger_path, clock)
    issued = store.create_ticket(
        resource_id="i-1",
        action=PotentialAction.STOP_RESOURCE,
        ticket_id=TICKET,
    )
    granted = store.grant(TICKET)
    with pytest.raises(RevisionConflictError):
        store.consume(TICKET, expected_revision=issued.revision)
    survivor = store.get(TICKET)
    assert survivor.status is ApprovalStatus.GRANTED
    assert survivor.revision == granted.revision
    store.verify()
    store.close()


def test_future_revision_refuses_in_memory() -> None:
    store = _memory()
    granted = _grant(store)
    with pytest.raises(RevisionConflictError):
        store.consume(TICKET, expected_revision=granted.revision + 7)
    assert store.get(TICKET).status is ApprovalStatus.GRANTED


def test_future_revision_refuses_durably(ledger_path: Path, clock: FakeClock) -> None:
    store = _durable(ledger_path, clock)
    granted = _grant(store)
    with pytest.raises(RevisionConflictError):
        store.consume(TICKET, expected_revision=granted.revision + 7)
    store.close()


def test_revoked_ticket_cannot_be_consumed_at_any_revision() -> None:
    """Revocation is terminal; a matching revision must not resurrect it."""
    store = _memory()
    _grant(store)
    store.revoke(TICKET, decided_by="bob")
    revoked = store.get(TICKET)
    with pytest.raises(InvalidTransitionError):
        store.consume(TICKET, expected_revision=revoked.revision)
    assert store.get(TICKET).status is ApprovalStatus.REVOKED


# --- existing semantics unchanged -------------------------------------------


def test_omitted_revision_preserves_compatibility_in_memory() -> None:
    store = _memory()
    _grant(store)
    assert store.consume(TICKET).status is ApprovalStatus.CONSUMED


def test_omitted_revision_preserves_compatibility_durably(
    ledger_path: Path, clock: FakeClock
) -> None:
    store = _durable(ledger_path, clock)
    _grant(store)
    assert store.consume(TICKET).status is ApprovalStatus.CONSUMED
    store.close()


def test_second_consume_is_still_an_invalid_transition() -> None:
    """Consumption stays exactly-once; the revision CAS does not change that."""
    store = _memory()
    granted = _grant(store)
    store.consume(TICKET, expected_revision=granted.revision)
    with pytest.raises(InvalidTransitionError):
        store.consume(TICKET, expected_revision=granted.revision + 1)


def test_pending_ticket_cannot_be_consumed() -> None:
    store = _memory()
    store.create_ticket(
        resource_id="i-1",
        action=PotentialAction.STOP_RESOURCE,
        ticket_id=TICKET,
    )
    pending = store.get(TICKET)
    with pytest.raises(InvalidTransitionError):
        store.consume(TICKET, expected_revision=pending.revision)
    assert store.get(TICKET).status is ApprovalStatus.PENDING


def test_unknown_ticket_raises() -> None:
    with pytest.raises(UnknownTicketError):
        _memory().consume("nope", expected_revision=1)


def test_transition_table_is_untouched() -> None:
    """The revision check is a precondition, never a new transition."""
    from sws_agent.approval import LEGAL_APPROVAL_TRANSITIONS

    assert LEGAL_APPROVAL_TRANSITIONS[ApprovalStatus.CONSUMED] == frozenset()
    assert LEGAL_APPROVAL_TRANSITIONS[ApprovalStatus.REVOKED] == frozenset()
    assert ApprovalStatus.CONSUMED in LEGAL_APPROVAL_TRANSITIONS[ApprovalStatus.GRANTED]


# --- execution-ledger binding regression ------------------------------------
# The intended Phase 4 contract, demonstrated with the smallest isolated
# objects: no coordinator, no handler, no execution wiring.


def test_reserved_revision_is_redeemable_and_a_moved_ticket_is_not(
    tmp_path: Path, clock: FakeClock
) -> None:
    """``consume(expected_revision=reservation.ticket_revision)`` is the seam.

    Phase 4 will reserve against a ticket, then redeem exactly that revision.
    Here the ledger records revision *R*; the approval store still answers
    "am I at *R*?" Yes. After the ticket advances, the same call is refused
    rather than silently redeeming the newer revision -- which is the TOCTOU
    hole this contract closes.

    The two stores are never joined. The ledger is not given a reference to the
    approval store, and it does not learn whether a ticket is authorized.
    """
    approvals = _durable(tmp_path / "approval.sqlite3", clock)
    ledger = DurableExecutionLedger(tmp_path / "execution.sqlite3", now=clock)
    try:
        issued = approvals.create_ticket(
            resource_id="i-1",
            action=PotentialAction.STOP_RESOURCE,
            ticket_id=TICKET,
            execution_intent_key=INTENT,
        )
        reservation = ledger.reserve(
            intent_key=INTENT,
            ticket_id=TICKET,
            ticket_revision=issued.revision,
            worker_id="w1",
            action=PotentialAction.STOP_RESOURCE,
            resource_id="i-1",
        )
        # The two facts agree, and redemption is pinned to that revision.
        assert reservation.ticket_revision == issued.revision

        granted = approvals.grant(TICKET)
        assert granted.revision != reservation.ticket_revision

        with pytest.raises(RevisionConflictError):
            approvals.consume(TICKET, expected_revision=reservation.ticket_revision)
        assert approvals.get(TICKET).status is ApprovalStatus.GRANTED

        # Re-read the reservation: the ledger still asserts its original
        # revision, and the store now agrees the redemption moved on.
        assert ledger.get(INTENT, TICKET).ticket_revision == issued.revision
        assert approvals.get(TICKET).revision == granted.revision

        # Reserving against the current revision is accepted, and the ledger
        # binds whatever revision it was handed. This second reservation uses a
        # distinct intent on purpose: Phase 4 refuses a fresh ticket for an
        # intent whose first execution is still open, and that guard is about
        # effect duplication, which is orthogonal to the revision binding this
        # test exists to pin. Mixing the two here would make a legitimate
        # revision assertion fail for an unrelated reason.
        fresh = ledger.reserve(
            intent_key=INTENT + "-other",
            ticket_id=TICKET + "-2",
            ticket_revision=granted.revision,
            worker_id="w1",
        )
        assert (
            approvals.consume(
                TICKET, expected_revision=fresh.ticket_revision
            ).status
            is ApprovalStatus.CONSUMED
        )
        ledger.verify()
        approvals.verify()
    finally:
        ledger.close()
        approvals.close()


def test_execution_ledger_has_no_approval_dependency() -> None:
    """Q2 stays unsolved on purpose: the ledger never becomes an authority.

    Checks imports rather than raw text: ``DurableApprovalStore`` appears in
    the module's *docstrings* to explain why the stores are separate, which is
    documentation, not a dependency. What must not exist is a real reference.
    """
    import ast
    import inspect

    import sws_agent.execution_ledger as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not any("approval" in name for name in imported), imported
    # Only stdlib plus the project's own vocabulary module.
    assert "constants" in imported
    assert "sqlite3" in imported