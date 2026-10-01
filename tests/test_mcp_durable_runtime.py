"""Runtime proof that the durable approval ledger is the real authority.

Every test here goes through the actual composition path -- ``main()``'s
``approval_store_from_env()`` resolver feeding ``DefaultSwsBackend`` -- and
then reads back through backend methods that the MCP approval tools call. The
point of the phase is that SQLite state survives *backend recreation*, so no
test may reuse a single store object: each scenario closes one store and
opens a fresh one against the same file. Reusing an object would pass even if
durability were completely broken.

No AWS, no network, no subprocesses, no credentials. SQLite files live in
``tmp_path``.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sws_agent.approval import (
    DuplicateTicketError,
    InMemoryApprovalStore,
    UnknownTicketError,
)
from sws_agent.approval_ledger import DurableApprovalStore
from sws_agent.audit import AuditRecordKind, JsonlAuditStore
from sws_agent.config import SWS_APPROVAL_DB_ENV, approval_db_from_env
from sws_agent.constants import (
    ExecutionMode,
    PotentialAction,
    SWSResourceType,
)
from sws_agent.execution import (
    ExecutionCoordinator,
    ExecutionRequest,
    RefusalReason,
)
from sws_agent.mcp.server import (
    BUILTIN_TOOL_NAMES,
    DefaultSwsBackend,
    SwsMcpServer,
    approval_store_from_env,
)
from sws_agent.models import ApprovalTicket

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeClock:
    """Deterministic clock so expiry can be exercised without sleeping."""

    def __init__(self, current: datetime = START) -> None:
        self._current = current

    def now(self) -> datetime:
        return self._current

    def advance(self, **delta) -> None:
        self._current = self._current + timedelta(**delta)


def _durable(
    path: Path, *, clock: FakeClock | None = None
) -> DurableApprovalStore:
    return DurableApprovalStore(path, now=(clock or FakeClock()).now)


def _store_for_env(path: Path, *, clock: FakeClock | None = None):
    """Open the store the runtime would select for ``SWS_APPROVAL_DB``.

    Path selection always goes through ``approval_store_from_env()`` so the
    env -> resolver -> store chain is what is under test. When a fake clock is
    requested the store is constructed directly instead, because the resolver
    deliberately exposes no clock seam -- that keeps expiry testable without
    wall-clock sleeps, and store selection is covered by its own tests.
    """
    import os

    os.environ[SWS_APPROVAL_DB_ENV] = str(path)
    try:
        if clock is not None:
            resolved = approval_db_from_env()
            assert resolved == path
            return DurableApprovalStore(resolved, now=clock.now)
        store = approval_store_from_env()
    finally:
        del os.environ[SWS_APPROVAL_DB_ENV]
    assert isinstance(store, DurableApprovalStore)
    return store


def _runtime_backend(path: Path, *, clock: FakeClock | None = None):
    """Build a backend the way ``main()`` does, via the env-driven resolver."""
    store = _store_for_env(path, clock=clock)
    backend = DefaultSwsBackend(
        approval_store=store, execution_mode=ExecutionMode.SAFE
    )
    return backend, store


def _request(
    backend: DefaultSwsBackend, resource_id: str = "fn-1"
) -> ApprovalTicket:
    """Create a ticket through the backend method the MCP tool calls."""
    plan = backend.request_approval(
        resource_id=resource_id,
        resource_type=SWSResourceType.LAMBDA_FUNCTION,
        action=PotentialAction.STOP_RESOURCE,
        rationale="deploy window",
    )
    return plan.ticket


# --- store selection -------------------------------------------------------


def test_unset_env_selects_no_store(monkeypatch):
    """Absent means the backend keeps its in-memory default."""
    monkeypatch.delenv(SWS_APPROVAL_DB_ENV, raising=False)
    assert approval_store_from_env() is None
    backend = DefaultSwsBackend(approval_store=approval_store_from_env())
    assert isinstance(backend._approval_store, InMemoryApprovalStore)


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", " \t "])
def test_blank_env_selects_no_store(monkeypatch, blank):
    """Blank and whitespace-only are both 'not configured'."""
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, blank)
    assert approval_store_from_env() is None
    backend = DefaultSwsBackend(approval_store=approval_store_from_env())
    assert isinstance(backend._approval_store, InMemoryApprovalStore)


def test_absolute_path_selects_durable_store(monkeypatch, tmp_path):
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, str(tmp_path / "approvals.sqlite3"))
    store = approval_store_from_env()
    assert isinstance(store, DurableApprovalStore)
    assert store.path == tmp_path / "approvals.sqlite3"
    store.close()


def test_relative_path_is_rejected_not_resolved(monkeypatch):
    """No silent relativization: the resolver fails instead."""
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, "relative/approvals.sqlite3")
    with pytest.raises(ValueError, match="absolute path"):
        approval_store_from_env()


def test_resolver_creates_the_database_file(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "approvals.sqlite3"
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, str(target))
    store = approval_store_from_env()
    try:
        assert target.exists()
        assert store.path == target
    finally:
        store.close()


# --- no silent fallback ----------------------------------------------------


def test_non_sqlite_file_aborts_without_falling_back(monkeypatch, tmp_path):
    """A garbage file is a startup failure, not an invitation to use memory.

    The dangerous outcome here is a server that answers approvals from RAM
    after the operator asked for durability. The resolver must raise so the
    process dies at startup instead.
    """
    corrupt = tmp_path / "approvals.sqlite3"
    corrupt.write_bytes(b"this is not a sqlite database at all")
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, str(corrupt))

    with pytest.raises(Exception) as caught:
        approval_store_from_env()
    assert not isinstance(caught.value, AssertionError)
    # The unreadable file is left exactly as found: never repaired or replaced.
    assert corrupt.read_bytes() == b"this is not a sqlite database at all"


def test_directory_in_place_of_database_aborts(monkeypatch, tmp_path):
    """A path occupied by a directory cannot be silently worked around."""
    occupied = tmp_path / "approvals.sqlite3"
    occupied.mkdir()
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, str(occupied))
    with pytest.raises(Exception):
        approval_store_from_env()
    assert occupied.is_dir()


def test_foreign_sqlite_schema_aborts(monkeypatch, tmp_path):
    """A valid SQLite file with a foreign schema is refused, not adopted."""
    foreign = tmp_path / "approvals.sqlite3"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, str(foreign))
    with pytest.raises(Exception):
        approval_store_from_env()


def test_parent_that_is_a_file_aborts(monkeypatch, tmp_path):
    """An unusable parent path fails instead of being repaired or replaced."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, str(blocker / "approvals.sqlite3"))
    with pytest.raises(Exception):
        approval_store_from_env()
    assert blocker.read_text() == "not a directory"


def test_tampered_ledger_aborts_on_reopen(tmp_path):
    """A previously valid ledger that is later damaged fails closed."""
    path = tmp_path / "approvals.sqlite3"
    first = _durable(path)
    first.create_ticket("fn-1", PotentialAction.STOP_RESOURCE, ticket_id="t1")
    first.close()

    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE ticket")
    conn.commit()
    conn.close()

    with pytest.raises(Exception):
        _durable(path)


# --- persistence across backend recreation ---------------------------------


def test_request_persists_across_backend_recreation(tmp_path):
    """Scenario A: a ticket issued by one backend is visible to the next."""
    path = tmp_path / "approvals.sqlite3"

    backend_a, store_a = _runtime_backend(path)
    ticket = _request(backend_a)
    store_a.close()

    backend_b, store_b = _runtime_backend(path)
    pending = {t.ticket_id: t for t in backend_b.list_approvals()}
    assert ticket.ticket_id in pending
    assert pending[ticket.ticket_id].status.value == "pending"
    assert pending[ticket.ticket_id].resource_id == "fn-1"
    assert pending[ticket.ticket_id].rationale == "deploy window"
    store_b.close()


def test_grant_persists_with_revision_and_deadline(tmp_path):
    """Scenario B: status, revision, and the stamped deadline all survive."""
    path = tmp_path / "approvals.sqlite3"

    backend_a, store_a = _runtime_backend(path)
    ticket = _request(backend_a)
    granted = backend_a.decide_ticket(
        ticket.ticket_id, decision="grant", decided_by="alice"
    )
    assert granted.status.value == "granted"
    assert granted.execution_deadline is not None
    store_a.close()

    backend_b, store_b = _runtime_backend(path)
    try:
        reopened = store_b.get(ticket.ticket_id)
        assert reopened.status.value == "granted"
        assert reopened.revision == granted.revision
        assert reopened.decided_by == "alice"
        assert reopened.execution_deadline == granted.execution_deadline
        # A granted ticket is no longer pending, and is not re-pending.
        assert backend_b.list_approvals() == []
        # The reopen is not a re-grant; the revision did not move.
        with pytest.raises(Exception):
            backend_b.decide_ticket(ticket.ticket_id, decision="grant")
    finally:
        store_b.close()


def test_denial_persists_and_cannot_be_overwritten(tmp_path):
    """A denial is durable too: it survives and blocks a later grant."""
    path = tmp_path / "approvals.sqlite3"

    backend_a, store_a = _runtime_backend(path)
    ticket = _request(backend_a)
    backend_a.decide_ticket(ticket.ticket_id, decision="deny", decided_by="bob")
    store_a.close()

    backend_b, store_b = _runtime_backend(path)
    try:
        reopened = store_b.get(ticket.ticket_id)
        assert reopened.status.value == "denied"
        assert reopened.decided_by == "bob"
        with pytest.raises(Exception):
            backend_b.decide_ticket(ticket.ticket_id, decision="grant")
        assert store_b.get(ticket.ticket_id).status.value == "denied"
    finally:
        store_b.close()


def test_expiry_is_materialized_and_durable(tmp_path):
    """Scenario D: expiry observed in one process persists for the next.

    Clock A is past the TTL, so process A observes the ticket as expired.
    Process B reopens the same file with a clock still past the TTL and must
    see EXPIRED state on disk, not merely recompute it from a live ticket.
    """
    path = tmp_path / "approvals.sqlite3"

    clock_a = FakeClock()
    backend_a, store_a = _runtime_backend(path, clock=clock_a)
    ticket = _request(backend_a)
    store_a.close()

    clock_b = FakeClock()
    clock_b.advance(hours=48)
    backend_b, store_b = _runtime_backend(path, clock=clock_b)
    try:
        assert backend_b.list_approvals() == []
        expired = store_b.get(ticket.ticket_id)
        assert expired.status.value == "expired"
    finally:
        store_b.close()

    # A third reopen on a later clock still reads EXPIRED off disk.
    backend_c, store_c = _runtime_backend(path, clock=FakeClock())
    try:
        assert store_c.get(ticket.ticket_id).status.value == "expired"
    finally:
        store_c.close()


def test_consume_persists_across_backend_recreation(tmp_path):
    """Scenario C, at the closest reachable boundary.

    ``consume`` is not exposed through MCP -- there is deliberately no
    execution tool -- and ``ExecutionCoordinator`` is not constructed by the
    MCP backend, so this cannot be driven from the MCP tool surface. It is
    exercised directly on the durable store, which is the same object the
    runtime hands the backend.
    """
    path = tmp_path / "approvals.sqlite3"

    backend_a, store_a = _runtime_backend(path)
    ticket = _request(backend_a)
    backend_a.decide_ticket(ticket.ticket_id, decision="grant", decided_by="alice")
    store_a.close()

    backend_b, store_b = _runtime_backend(path)
    try:
        consumed = store_b.consume(ticket.ticket_id)
        assert consumed.status.value == "consumed"
    finally:
        store_b.close()

    backend_c, store_c = _runtime_backend(path)
    try:
        reopened = store_c.get(ticket.ticket_id)
        assert reopened.status.value == "consumed"
        # Consumed is terminal: it cannot be granted again after a restart.
        with pytest.raises(Exception):
            store_c.grant(ticket.ticket_id, decided_by="alice")
    finally:
        store_c.close()


# --- two backends, one ledger ---------------------------------------------


def test_two_backends_share_one_ledger(tmp_path):
    """Scenario E: independent store objects over one file see each other."""
    path = tmp_path / "approvals.sqlite3"

    backend_a, store_a = _runtime_backend(path)
    backend_b, store_b = _runtime_backend(path)

    # Distinct store objects -- otherwise this would prove nothing.
    assert store_a is not store_b
    assert store_a.path == store_b.path
    # ...but the same underlying file.
    with sqlite3.connect(path) as probe:
        assert probe.execute("SELECT COUNT(*) FROM ticket").fetchone()[0] == 0

    ticket = _request(backend_a)
    assert [t.ticket_id for t in backend_b.list_approvals()] == [ticket.ticket_id]

    backend_a.decide_ticket(ticket.ticket_id, decision="grant", decided_by="alice")

    seen = store_b.get(ticket.ticket_id)
    assert seen.status.value == "granted"
    assert backend_b.list_approvals() == []

    store_a.close()
    store_b.close()


def test_stale_concurrent_decision_produces_one_transition(tmp_path):
    """A second decision on an already-decided ticket cannot also win."""
    path = tmp_path / "approvals.sqlite3"

    backend_a, store_a = _runtime_backend(path)
    backend_b, store_b = _runtime_backend(path)

    ticket = _request(backend_a)
    winner = backend_a.decide_ticket(
        ticket.ticket_id, decision="grant", decided_by="alice"
    )
    with pytest.raises(Exception):
        backend_b.decide_ticket(ticket.ticket_id, decision="deny", decided_by="bob")

    final = store_b.get(ticket.ticket_id)
    assert final.status.value == winner.status.value == "granted"
    assert final.revision == winner.revision
    assert final.decided_by == "alice"

    store_a.close()
    store_b.close()


def test_concurrent_creation_of_same_ticket_id_yields_one_winner(tmp_path):
    """Duplicate identity is enforced across processes, not just in memory."""
    path = tmp_path / "approvals.sqlite3"
    store_a = _durable(path)
    store_a.create_ticket("fn-1", PotentialAction.STOP_RESOURCE, ticket_id="same")
    store_a.close()

    store_b = _durable(path)
    try:
        with pytest.raises(DuplicateTicketError):
            store_b.create_ticket(
                "fn-2", PotentialAction.STOP_RESOURCE, ticket_id="same"
            )
        assert store_b.get("same").resource_id == "fn-1"
    finally:
        store_b.close()


# --- duplicate-ticket parity across both stores ----------------------------


def test_both_stores_refuse_the_same_duplicate_ticket_id(tmp_path):
    """Scenario F: the Phase 3A parity prerequisite still holds."""
    durable_store = _durable(tmp_path / "approvals.sqlite3")
    memory_store = InMemoryApprovalStore(now=FakeClock().now)
    for store in (durable_store, memory_store):
        store.create_ticket(
            "fn-1", PotentialAction.STOP_RESOURCE, "run A", ticket_id="dup"
        )
        store.grant("dup", decided_by="alice")
        with pytest.raises(DuplicateTicketError):
            store.create_ticket(
                "fn-2",
                PotentialAction.STOP_RESOURCE,
                "run B",
                ticket_id="dup",
            )
        survivor = store.get("dup")
        assert survivor.status.value == "granted"
        assert survivor.revision == 1
        assert survivor.decided_by == "alice"
    durable_store.close()


# --- duplicate parity survives a restart ---------------------------------


def test_duplicate_refusal_persists_after_reopen(tmp_path):
    """A duplicate refused by the durable store stays refused on reopen."""
    path = tmp_path / "approvals.sqlite3"

    backend_a, store_a = _runtime_backend(path)
    _request(backend_a, resource_id="fn-1")
    store_a.close()

    backend_b, store_b = _runtime_backend(path)
    try:
        existing = store_b.pending()[0]
        with pytest.raises(DuplicateTicketError):
            store_b.create_ticket(
                "fn-2",
                PotentialAction.STOP_RESOURCE,
                ticket_id=existing.ticket_id,
            )
        assert store_b.get(existing.ticket_id).resource_id == "fn-1"
    finally:
        store_b.close()


def test_unknown_ticket_survives_as_unknown_after_reopen(tmp_path):
    """Identity that was never issued is still unknown in a new process."""
    path = tmp_path / "approvals.sqlite3"
    backend_a, store_a = _runtime_backend(path)
    _request(backend_a)
    store_a.close()

    backend_b, store_b = _runtime_backend(path)
    try:
        with pytest.raises(UnknownTicketError):
            store_b.get("never-issued")
    finally:
        store_b.close()


# --- cross-process proof ---------------------------------------------------


def _child_reads_ticket(
    db_path: str, ticket_id: str, out: multiprocessing.Queue
) -> None:
    """Top-level target: resolve the store from the env, read, report.

    Runs in a genuinely separate interpreter, so this proves the on-disk file
    is the authority rather than any in-process object.
    """
    import os

    os.environ[SWS_APPROVAL_DB_ENV] = db_path
    try:
        store = approval_store_from_env()
        ticket = store.get(ticket_id)
        out.put(("ok", ticket.status.value, ticket.revision))
        store.close()
    except Exception as exc:  # pragma: no cover - diagnostic path
        out.put(("error", type(exc).__name__, str(exc)))


def test_ticket_is_visible_to_a_separate_process(tmp_path):
    """Backend A issues; a fresh interpreter resolves the same path and reads."""
    path = tmp_path / "approvals.sqlite3"

    backend_a, store_a = _runtime_backend(path)
    ticket = _request(backend_a)
    backend_a.decide_ticket(ticket.ticket_id, decision="grant", decided_by="alice")
    granted = store_a.get(ticket.ticket_id)
    store_a.close()

    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    proc = ctx.Process(
        target=_child_reads_ticket, args=(str(path), ticket.ticket_id, out)
    )
    proc.start()
    proc.join(120)
    assert proc.exitcode == 0, "child process failed"

    outcome, status, revision = out.get(timeout=5)
    assert outcome == "ok", f"child reported {status!r} (revision={revision!r})"
    assert status == "granted"
    assert revision == granted.revision


# --- MCP surface after migration ------------------------------------------


def test_tool_surface_unchanged_with_durable_store(tmp_path):
    """Migration changes the authority, not the exposed surface."""
    backend, store = _runtime_backend(tmp_path / "approvals.sqlite3")
    try:
        server = SwsMcpServer(backend=backend)
        assert len(server.registry.names()) == len(BUILTIN_TOOL_NAMES) == 9
        assert set(server.registry.names()) == set(BUILTIN_TOOL_NAMES)
        names = set(server.registry.names())
        assert not names & {"execute_action", "apply_changes", "stop_resource"}
        assert {"request_approval", "list_approvals", "decide_ticket"} <= names
    finally:
        store.close()


def test_approval_tools_use_the_durable_store(tmp_path):
    """The MCP approval tools operate on the selected (durable) store."""
    import asyncio

    backend, store = _runtime_backend(tmp_path / "approvals.sqlite3")
    server = SwsMcpServer(backend=backend)

    async def _call(tool, args):
        return await server.call_tool(tool, args)

    def _payload(result) -> dict:
        import json

        content = result.content[0]
        structured = getattr(content, "structured_content", None)
        if structured is not None:
            return json.loads(structured) if isinstance(structured, str) else structured
        return json.loads(content.text)

    try:
        result = asyncio.run(
            _call(
                "request_approval",
                {
                    "resource_id": "fn-1",
                    "resource_type": "lambda_function",
                    "action": "stop_resource",
                    "rationale": "deploy window",
                },
            )
        )
        assert result.is_error is False

        listed = asyncio.run(_call("list_approvals", {}))
        assert listed.is_error is False
        payload = _payload(listed)
        assert len(payload["approvals"]) == 1
        ticket_id = payload["approvals"][0]["ticket_id"]

        decided = asyncio.run(
            _call(
                "decide_ticket",
                {"ticket_id": ticket_id, "decision": "grant", "decided_by": "alice"},
            )
        )
        assert decided.is_error is False
        # The grant is on disk, not in the backend object.
        assert store.get(ticket_id).status.value == "granted"
    finally:
        store.close()


# --- execution authority compatibility ------------------------------------


def test_execution_coordinator_reads_the_durable_store(tmp_path):
    """Inspection-only: the ticket gate resolves its authority from SQLite.

    ``ExecutionCoordinator`` is not wired into MCP and M13 ordering
    (handler then consume) is untouched. This confirms a coordinator built on
    the durable store consults that store for ticket state -- including after
    a reopen, where nothing in memory survives.

    This test found a latent defect, since fixed: the plan binding check in
    ``execution.py`` compared ``plan_id`` with ``is not``, i.e. by object
    identity. ``InMemoryApprovalStore`` returns its own cached instance so the
    comparison happened to hold; ``DurableApprovalStore`` rehydrates from
    SQLite, so the plan id the gate re-read was always a distinct ``str`` and
    every durable approval was refused. The gate now compares values, and this
    test pins the corrected behavior through the runtime-composed store.
    """
    path = tmp_path / "approvals.sqlite3"
    backend, store = _runtime_backend(path)
    try:
        ticket = _request(backend)
        backend.decide_ticket(ticket.ticket_id, decision="grant", decided_by="alice")

        coordinator = ExecutionCoordinator(approval_store=store)
        assert coordinator._store is store

        # The gate's authority is the durable store, not backend state.
        assert coordinator._store.get(ticket.ticket_id).status.value == "granted"
    finally:
        try:
            store.close()
        except Exception:
            pass

    # After reopen there is no in-memory ticket at all: state came off disk.
    reopened = _durable(path)
    try:
        reloaded = ExecutionCoordinator(approval_store=reopened)
        assert reloaded._store is reopened
        recovered = reopened.get(ticket.ticket_id)
        assert recovered.status.value == "granted"
        assert recovered.revision == 1

        # The store is consulted, and status is read from SQLite, not memory.
        assert reloaded._store.get(ticket.ticket_id).status.value == "granted"

        # A GRANTED durable ticket survives the reopen and passes plan binding.
        # The plan ids are equal by value but distinct by identity, so this only
        # holds because the comparison was corrected to value equality.
        gate_args = dict(
            resource_id=recovered.resource_id,
            action=recovered.action,
            execution_mode=ExecutionMode.SAFE,
            ticket=recovered,
        )
        request = ExecutionRequest(
            action_plan_id=recovered.plan_id, **gate_args
        )
        # Equal by value -- the values genuinely match; only identity differs.
        assert request.action_plan_id == reloaded._store.get(
            ticket.ticket_id
        ).plan_id
        assert request.action_plan_id is not reloaded._store.get(
            ticket.ticket_id
        ).plan_id
        assert reloaded._ticket_gate(request) is None

        # A genuinely different plan id is still refused.
        wrong = ExecutionRequest(action_plan_id="not-this-plan", **gate_args)
        refusal = reloaded._ticket_gate(wrong)
        assert refusal is not None
        assert refusal[0] is RefusalReason.TICKET_MISMATCH_PLAN
    finally:
        reopened.close()


def test_plan_binding_also_passes_on_the_in_memory_store():
    """Contrasting probe: in-memory semantics are unchanged by the fix.

    This store always returned the identical object, so it was the case that
    masked the identity-comparison bug.
    """
    store = InMemoryApprovalStore()
    backend = DefaultSwsBackend(
        approval_store=store, execution_mode=ExecutionMode.SAFE
    )
    ticket = _request(backend)
    backend.decide_ticket(ticket.ticket_id, decision="grant", decided_by="alice")

    granted = store.get(ticket.ticket_id)
    request = ExecutionRequest(
        action_plan_id=granted.plan_id,
        resource_id=granted.resource_id,
        action=granted.action,
        execution_mode=ExecutionMode.SAFE,
        ticket=granted,
    )
    assert ExecutionCoordinator(approval_store=store)._ticket_gate(request) is None


# --- audit compatibility ---------------------------------------------------


def test_ticket_audit_records_unaffected_by_storage_change(tmp_path):
    """Audit payload shape is identical for memory and durable backends."""
    memory_audit = JsonlAuditStore(tmp_path / "memory" / "audit.jsonl")
    memory_backend = DefaultSwsBackend(audit_store=memory_audit)
    memory_ticket = _request(memory_backend)

    db_path = tmp_path / "approvals.sqlite3"
    durable_audit = JsonlAuditStore(tmp_path / "durable" / "audit.jsonl")
    backend, store = _runtime_backend(db_path)
    durable_backend = DefaultSwsBackend(
        audit_store=durable_audit, approval_store=store
    )
    try:
        durable_ticket = _request(durable_backend)

        memory_records = [
            r for r in memory_audit.records() if r.kind is AuditRecordKind.TICKET
        ]
        durable_records = [
            r for r in durable_audit.records() if r.kind is AuditRecordKind.TICKET
        ]
        assert len(memory_records) == len(durable_records) == 1

        # Same envelope shape and payload schema: only the id values differ.
        left, right = memory_records[0], durable_records[0]
        assert left.kind is right.kind is AuditRecordKind.TICKET
        assert left.schema_version == right.schema_version == 1
        assert left.execution is None and right.execution is None
        assert set(left.payload) == set(right.payload)
        assert left.payload["status"] == right.payload["status"] == "pending"
        assert left.payload["resource_id"] == right.payload["resource_id"]
        assert left.ticket_id == memory_ticket.ticket_id
        assert right.ticket_id == durable_ticket.ticket_id
    finally:
        store.close()