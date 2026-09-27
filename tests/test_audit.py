"""M8 durable audit ledger: hermetic tests for the JSONL store + write-through.

No network, no AWS, no credentials, no boto3, no LLM. The store is exercised
against ``tmp_path``; the backend write-through against ``DefaultSwsBackend``
driven by an in-memory fake AWS client, and the MCP surface through
``SwsMcpServer.call_tool`` exactly like ``test_mcp_server.py``. Real audit
data is never written into the repository: every ledger lives under a pytest
``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from sws_agent.approval import InMemoryApprovalStore
from sws_agent.audit import (
    AUDIT_SCHEMA_VERSION,
    AuditRecordKind,
    AuditStoreError,
    ClosedAuditStoreError,
    CorruptLedgerError,
    DuplicateRecordError,
    JsonlAuditStore,
    cost_payload,
    explanation_payload,
    snapshot_payload,
)
from sws_agent.constants import PotentialAction, SWSResourceType
from sws_agent.mcp.server import DefaultSwsBackend, SwsMcpServer
from sws_agent.models import (
    CostCollectionReport,
    ResourceRecord,
    WorkspaceSnapshot,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class SeqIds:
    """Deterministic, non-empty, collision-prone-when-asked id source."""

    def __init__(self, prefix: str = "rec") -> None:
        self._n = 0
        self._prefix = prefix
        self._collide = False

    def __call__(self) -> str:
        if self._collide:
            return f"{self._prefix}-0001"
        self._n += 1
        return f"{self._prefix}-{self._n:04d}"


def _clock() -> datetime:
    return NOW


def _store(path, **kwargs) -> JsonlAuditStore:
    kwargs.setdefault("now", _clock)
    kwargs.setdefault("id_source", SeqIds())
    return JsonlAuditStore(path, **kwargs)


# ---------------------------------------------------------------------------
# 8.1 store: append-only JSONL, stamped identity, deterministic behavior
# ---------------------------------------------------------------------------


def test_store_round_trips_deterministically(tmp_path):
    store = _store(tmp_path / "audit.jsonl")
    first = store.write(
        AuditRecordKind.RUN, run_id="r-1", payload={"k": 1}
    )
    second = store.write(
        AuditRecordKind.SNAPSHOT,
        run_id="r-1",
        snapshot_id="s-1",
        payload={"v": 2},
    )
    assert len(store) == 2
    assert first.record_id == "rec-0001"
    assert second.record_id == "rec-0002"
    assert first.created_at == NOW
    assert second.created_at == NOW
    assert first.kind is AuditRecordKind.RUN
    assert second.snapshot_id == "s-1"
    # Deterministic replay order and identical replayed envelopes.
    assert store.records() == [first, second]

    reopened = _store(tmp_path / "audit.jsonl")
    assert len(reopened) == 2
    assert reopened.records() == store.records()
    assert reopened.records()[0].payload == {"k": 1}
    reopened.close()


def test_store_is_append_only_utf8_one_line_per_record(tmp_path):
    store = _store(tmp_path / "audit.jsonl")
    store.write(AuditRecordKind.RUN, run_id="r-1")
    store.write(AuditRecordKind.DECISION, decision_id="d-1")
    store.close()
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)
        assert parsed["schema_version"] == AUDIT_SCHEMA_VERSION
        assert parsed["created_at"]
        assert parsed["record_id"]


def test_store_duplicate_record_id_raises(tmp_path):
    ids = SeqIds()
    ids._collide = True  # noqa: SLF001 - deliberate collision for this test
    store = _store(tmp_path / "audit.jsonl", id_source=ids)
    store.write(AuditRecordKind.RUN)
    with pytest.raises(DuplicateRecordError):
        store.write(AuditRecordKind.SNAPSHOT)


def test_store_duplicate_record_id_raises_across_reopen(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = _store(path, id_source=SeqIds())
    store.write(AuditRecordKind.RUN)
    store.close()
    with pytest.raises(DuplicateRecordError):
        # Reopened ledger already contains rec-0001; a fresh store re-writing
        # the same id must fail instead of producing a duplicate line.
        JsonlAuditStore(path, now=_clock, id_source=SeqIds()).write(
            AuditRecordKind.RUN
        )


def test_store_corrupt_line_fails_fast(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        '{"schema_version": 1, "record_id": "rec-1", "kind": "run", '
        '"created_at": "2026-03-01T12:00:00+00:00", "payload": {}}\n'
        "this is not json\n",
        encoding="utf-8",
    )
    with pytest.raises(CorruptLedgerError):
        JsonlAuditStore(path, now=_clock, id_source=SeqIds())


def test_store_missing_record_id_line_fails_fast(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        '{"schema_version": 1, "kind": "run", '
        '"created_at": "2026-03-01T12:00:00+00:00", "payload": {}}\n',
        encoding="utf-8",
    )
    with pytest.raises(CorruptLedgerError):
        JsonlAuditStore(path, now=_clock, id_source=SeqIds())


def test_store_write_after_close_raises(tmp_path):
    store = _store(tmp_path / "audit.jsonl")
    store.write(AuditRecordKind.RUN)
    store.close()
    with pytest.raises(ClosedAuditStoreError):
        store.write(AuditRecordKind.SNAPSHOT)


def test_store_naive_clock_fails_fast(tmp_path):
    def naive() -> datetime:
        return datetime(2026, 3, 1, 12, 0)

    with pytest.raises(ValueError):
        JsonlAuditStore(tmp_path / "audit.jsonl", now=naive, id_source=SeqIds())


def test_store_payloads_never_contain_forbidden_fields(tmp_path):
    snapshot = WorkspaceSnapshot(
        snapshot_id="rec-0001",
        created_at=NOW,
        regions=["us-east-1"],
        resource_types=[SWSResourceType.S3_BUCKET],
        resources=[
            ResourceRecord(
                resource_id="b-1",
                resource_type=SWSResourceType.S3_BUCKET,
                raw={"secret": "never-record-this", "wire": {"k": "v"}},
            )
        ],
    )
    payload = snapshot_payload(snapshot)
    assert "secret" not in json.dumps(payload)
    resource = payload["resources"][0]
    assert "raw" not in resource
    assert resource["resource_id"] == "b-1"
    assert resource["resource_type"] == "s3_bucket"


def test_explanation_payload_excludes_prose_text(tmp_path):
    payload = explanation_payload(
        resource_id="b-1",
        decision_id="d-1",
        snapshot_id="s-1",
        run_id="r-1",
        provider="spy",
        claim_kind="interpreted",
        reason="deterministic",
    )
    assert "text" not in payload
    assert payload["provider"] == "spy"
    assert payload["claim_kind"] == "interpreted"
    assert payload["reason"] == "deterministic"
    assert payload["decision_id"] == "d-1"


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def test_cost_payload_carries_intent_and_no_raw_pages():
    report = CostCollectionReport(
        estimates=[],
    )
    payload = cost_payload(
        end_date="2026-03-01",
        window_days=7,
        group_by=["SERVICE"],
        report=report,
    )
    assert payload["end_date"] == "2026-03-01"
    assert payload["window_days"] == 7
    assert payload["group_by"] == ["SERVICE"]
    assert payload["estimates"] == []
    assert payload["truncated"] is False
    assert payload["failures"] == []


# ---------------------------------------------------------------------------
# 8.2 backend write-through: collect/evaluate/plan/ticket/explain/cost
# ---------------------------------------------------------------------------


class FakeCostClient:
    def __init__(self, responses=None):
        self._responses = [dict(r) for r in (responses or [])]

    def get_cost_and_usage(self, **params):
        return (
            self._responses.pop(0)
            if self._responses
            else {"ResultsByTime": []}
        )


class FakeS3Client:
    def __init__(self, buckets=None):
        self.calls = []
        self.buckets = buckets or []

    def list_buckets(self):
        self.calls.append("list_buckets")
        return {"Buckets": self.buckets}

    def get_bucket_location(self, Bucket):
        return {"LocationConstraint": "us-east-1"}

    def get_bucket_tagging(self, Bucket):
        return {"TagSet": [{"Key": "Owner", "Value": "team-alpha"}]}


class FakeLambdaClient:
    def __init__(self):
        self.calls = []

    def list_functions(self, Marker=None):
        return {"Functions": []}

    def list_tags(self, Resource):
        return {"Tags": {}}


class FakeRootClient:
    def __init__(self, cost=None, buckets=None):
        self._s3 = FakeS3Client(buckets=buckets)
        self._lamb = FakeLambdaClient()
        self._cost = cost

    def list_buckets(self):
        return self._s3.list_buckets()

    def get_bucket_location(self, Bucket):
        return self._s3.get_bucket_location(Bucket)

    def get_bucket_tagging(self, Bucket):
        return self._s3.get_bucket_tagging(Bucket)

    def list_functions(self, Marker=None):
        return self._lamb.list_functions(Marker)

    def list_tags(self, Resource):
        return self._lamb.list_tags(Resource)

    def get_cost_and_usage(self, **params):
        if self._cost is None:
            raise AssertionError("get_cost_and_usage called without cost client")
        return self._cost.get_cost_and_usage(**params)


class FailingStore:
    """Audit-store fake whose every write fails loudly (fail-loud contract).

    Deliberately does NOT define ``__bool__``/``__len__`` so it stays
    truthy for the ``audit_store or _store(...)`` default-resolution in
    ``_backend``.
    """

    def write(self, *args, **kwargs):
        raise AuditStoreError("disk full")

    def records(self):
        return []

    def close(self):
        pass


def _backend(tmp_path, *, cost=None, audit_store=None, buckets=None) -> DefaultSwsBackend:
    store = audit_store or _store(tmp_path / "audit.jsonl")
    return DefaultSwsBackend(
        client_factory=lambda: FakeRootClient(cost=cost, buckets=buckets),
        audit_store=store,
    )


def test_collect_workspace_writes_run_and_snapshot(tmp_path):
    backend = _backend(tmp_path)
    snapshot = backend.collect_workspace(regions=["us-east-1"])
    records = backend._audit_store.records()  # noqa: SLF001 - test access
    assert [r.kind for r in records] == [
        AuditRecordKind.RUN,
        AuditRecordKind.SNAPSHOT,
    ]
    run, snap = records
    assert run.run_id == snapshot.run_id
    assert run.snapshot_id == snapshot.snapshot_id
    assert run.payload["run_type"] == "workspace"
    assert snap.snapshot_id == snapshot.snapshot_id
    assert snap.run_id == snapshot.run_id
    assert {t for t in snap.payload["counts"]} == {
        "s3_bucket",
        "lambda_function",
    }
    assert snap.payload["resources"] == []


def test_collect_without_store_writes_nothing(tmp_path):
    backend = DefaultSwsBackend(
        client_factory=lambda: FakeRootClient(),
    )
    snapshot = backend.collect_workspace(regions=["us-east-1"])
    assert snapshot.snapshot_id
    # No audit_store configured: nothing to inspect, nothing was written.


def test_evaluate_workspace_writes_one_decision_per_decision(tmp_path):
    backend = _backend(tmp_path)
    snapshot = backend.collect_workspace(regions=["us-east-1"])
    decisions = backend.evaluate_workspace(snapshot)
    records = backend._audit_store.records()
    kinds = [r.kind for r in records if r.kind is AuditRecordKind.DECISION]
    assert len(kinds) == len(snapshot.resources)
    for decision in decisions:
        record = next(
            r
            for r in records
            if r.kind is AuditRecordKind.DECISION
            and r.decision_id == decision.decision_id
        )
        assert record.resource_id == decision.resource_id
        assert record.snapshot_id == snapshot.snapshot_id
        assert record.run_id == snapshot.run_id


def test_decisions_are_deterministic_and_lineage_survives_replay(tmp_path):
    path = tmp_path / "audit.jsonl"
    backend = _backend(tmp_path, audit_store=JsonlAuditStore(path, now=_clock))
    snapshot = backend.collect_workspace(regions=["us-east-1"])
    first = backend.evaluate_workspace(snapshot)
    second = backend.evaluate_workspace(snapshot)
    assert [d.decision_id for d in first] == [d.decision_id for d in second]
    assert all(
        records_run_id == snapshot.run_id
        for record in backend._audit_store.records()
        if record.kind is AuditRecordKind.DECISION
        for records_run_id in [record.run_id]
    )
    # Reopened ledger reconstructs the lineage chain snapshot -> decisions.
    reopened = JsonlAuditStore(path, now=_clock)
    snapshot_record = next(
        r for r in reopened.records() if r.kind is AuditRecordKind.SNAPSHOT
    )
    decision_records = [
        r for r in reopened.records() if r.kind is AuditRecordKind.DECISION
    ]
    assert snapshot_record.snapshot_id == snapshot.snapshot_id
    assert all(
        d.snapshot_id == snapshot.snapshot_id and d.run_id == snapshot.run_id
        for d in decision_records
    )
    reopened.close()


def test_request_approval_writes_plan_and_ticket(tmp_path):
    backend = _backend(tmp_path)
    plan = backend.request_approval(
        resource_id="fn-1",
        resource_type=SWSResourceType.LAMBDA_FUNCTION,
        action=PotentialAction.STOP_RESOURCE,
        rationale="runtime",
    )
    assert plan.ticket is not None
    records = backend._audit_store.records()
    plan_record = next(r for r in records if r.kind is AuditRecordKind.PLAN)
    ticket_records = [
        r for r in records if r.kind is AuditRecordKind.TICKET
    ]
    assert len(ticket_records) == 1
    assert plan_record.action_plan_id == plan.action_plan_id
    assert plan_record.payload["authorization"]["decision"] == "pending_approval"
    assert plan_record.payload["executed"] is False
    assert ticket_records[0].ticket_id == plan.ticket.ticket_id
    assert ticket_records[0].action_plan_id == plan.action_plan_id
    assert ticket_records[0].resource_id == "fn-1"
    assert ticket_records[0].payload["status"] == "pending"


def test_request_approval_plan_without_ticket_is_valid(tmp_path):
    backend = _backend(tmp_path)
    plan = backend.request_approval(
        resource_id="b-1",
        resource_type=SWSResourceType.S3_BUCKET,
        action=PotentialAction.LEAVE,
    )
    assert plan.ticket is None
    kinds = [r.kind for r in backend._audit_store.records()]
    assert AuditRecordKind.PLAN in kinds
    assert AuditRecordKind.TICKET not in kinds


def test_decide_ticket_writes_transition_record(tmp_path):
    backend = _backend(tmp_path)
    plan = backend.request_approval(
        resource_id="fn-1",
        resource_type=SWSResourceType.LAMBDA_FUNCTION,
        action=PotentialAction.STOP_RESOURCE,
    )
    ticket = plan.ticket
    assert ticket is not None
    decided = backend.decide_ticket(ticket.ticket_id, decision="grant", decided_by="ops")
    tickets = [
        r
        for r in backend._audit_store.records()
        if r.kind is AuditRecordKind.TICKET
    ]
    assert [t.ticket_id for t in tickets] == [ticket.ticket_id, ticket.ticket_id]
    assert tickets[-1].payload["status"] == "granted"
    assert tickets[-1].payload["decided_by"] == "ops"
    assert tickets[-1].action_plan_id == plan.action_plan_id


def test_explain_resource_writes_explanation_without_text(tmp_path):
    backend = _backend(
        tmp_path,
        buckets=[
            {"Name": "b-1", "CreationDate": NOW},
        ],
    )
    snapshot = backend.collect_workspace(regions=["us-east-1"])
    assert snapshot.resources, "expected a seeded bucket resource"
    decisions = backend.evaluate_workspace(snapshot)
    target = next(d for d in decisions)
    resource = next(r for r in snapshot.resources if r.resource_id == target.resource_id)
    result = backend.explain(resource, target)
    record = next(
        r
        for r in backend._audit_store.records()
        if r.kind is AuditRecordKind.EXPLANATION
    )
    assert record.decision_id == target.decision_id
    assert record.snapshot_id == snapshot.snapshot_id
    assert record.resource_id == resource.resource_id
    assert record.payload["provider"] == "null"
    assert record.payload["claim_kind"] == result.claim_kind.value
    assert "text" not in record.payload


def test_get_cost_estimates_writes_run_and_cost(tmp_path):
    cost = FakeCostClient(
        responses=[
            {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-02-22", "End": "2026-03-01"},
                        "Total": {"UnblendedCost": {"Amount": "10.00"}},
                        "Groups": [],
                    }
                ]
            }
        ]
    )
    backend = _backend(tmp_path, cost=cost)
    report = backend.get_cost_estimates(
        end_date=date(2026, 3, 1), window_days=7, group_by=["service"]
    )
    records = backend._audit_store.records()
    run = next(r for r in records if r.kind is AuditRecordKind.RUN)
    cost_record = next(r for r in records if r.kind is AuditRecordKind.COST)
    assert run.payload["run_type"] == "cost"
    assert cost_record.run_id == run.run_id
    assert cost_record.payload["end_date"] == "2026-03-01"
    assert cost_record.payload["window_days"] == 7
    assert cost_record.payload["group_by"] == ["service"]
    assert len(cost_record.payload["estimates"]) == len(report.estimates)


def test_persistence_failure_surfaces_at_backend(tmp_path):
    backend = _backend(tmp_path, audit_store=FailingStore())
    with pytest.raises(AuditStoreError):
        backend.collect_workspace(regions=["us-east-1"])


def test_persistence_failure_surfaces_as_tool_error(tmp_path):
    from mcp.server.mcpserver.exceptions import ToolError

    server = SwsMcpServer(
        backend=_backend(
            tmp_path,
            audit_store=FailingStore(),
        )
    )
    with pytest.raises(ToolError) as exc:
        _run(server.call_tool("collect_workspace", {"regions": ["us-east-1"]}))
    assert "disk full" in str(exc.value)


# ---------------------------------------------------------------------------
# 8.2 MCP surface: shapes preserved, deterministic, end-to-end lineage
# ---------------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _payload(result) -> dict:
    content = result.content[0]
    structured = getattr(content, "structured_content", None)
    if structured is not None:
        if isinstance(structured, str):
            return json.loads(structured)
        return structured
    text = getattr(content, "text", None)
    if text is not None:
        return json.loads(text)
    raise AssertionError("tool result carried no JSON payload")


def test_audit_workspace_end_to_end_writes_ledger_and_preserves_shape(tmp_path):
    server = SwsMcpServer(
        backend=_backend(tmp_path),
    )
    payload = _payload(
        _run(server.call_tool("audit_workspace", {"regions": ["us-east-1"]}))
    )
    assert "snapshot" in payload
    assert "relationships" in payload
    assert "decisions" in payload
    snapshot = payload["snapshot"]
    assert snapshot["run_id"]
    assert all("decision_id" in d for d in payload["decisions"])
    records = server._backend._audit_store.records()  # noqa: SLF001
    kinds = [r.kind for r in records]
    assert kinds.count(AuditRecordKind.RUN) == 1
    assert kinds.count(AuditRecordKind.SNAPSHOT) == 1
    assert kinds.count(AuditRecordKind.DECISION) == len(snapshot["resources"])
    snapshot_record = next(
        r for r in records if r.kind is AuditRecordKind.SNAPSHOT
    )
    decision_ids = {
        r.decision_id
        for r in records
        if r.kind is AuditRecordKind.DECISION
    }
    assert {d["decision_id"] for d in payload["decisions"]} == decision_ids
    assert all(
        d["snapshot_id"] == snapshot_record.snapshot_id
        for d in payload["decisions"]
    )


def test_mcp_decision_records_are_unique_and_correlated(tmp_path):
    backend = _backend(tmp_path)
    snapshot = backend.collect_workspace(regions=["us-east-1"])
    backend.evaluate_workspace(snapshot)
    decision_records = [
        r
        for r in backend._audit_store.records()
        if r.kind is AuditRecordKind.DECISION
    ]
    assert len({r.decision_id for r in decision_records}) == len(decision_records)
    assert len({r.resource_id for r in decision_records}) == len(decision_records)


def test_no_store_backend_never_writes(tmp_path):
    backend = DefaultSwsBackend(
        client_factory=lambda: FakeRootClient(),
    )
    assert backend._audit_store is None  # noqa: SLF001