"""M4 MCP server: hermetic tests for the Streamable HTTP server surface.

The server (sws_agent.mcp.server) is transport/exposure only: every tool is a
thin adapter over the deterministic SWS core, and no action-execution tool is
exposed. These tests use injected fakes (seeded backend, spy explainer, real
in-memory approval store) and exercise the SDK dispatcher through
``SwsMcpServer.call_tool``. They require no AWS credentials and no network
listener.

Full transport-level (wire) validation lives in the local loopback validation
script under scripts/experiments (uvicorn on 127.0.0.1), which is where the
SDK's ``isError`` result conversion is observable end-to-end.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from sws_agent.approval import (
    ApprovalStoreCorruptionError,
    ApprovalStoreUnavailableError,
    InMemoryApprovalStore,
)
from sws_agent.config import SWS_APPROVAL_DB_ENV
from sws_agent.constants import (
    ExecutionMode,
    PotentialAction,
    RiskLevel,
    SWSResourceType,
)
from sws_agent.explanation import NullExplanationProvider
from sws_agent.mcp import MCPServer, ToolRegistry
from sws_agent.mcp.server import (
    BUILTIN_TOOL_NAMES,
    SERVER_DESCRIPTION,
    SERVER_NAME,
    SERVER_VERSION,
    DefaultSwsBackend,
    SwsBackend,
    SwsMcpServer,
    _jsonable,
)
from sws_agent.models import (
    ClaimKind,
    CostCollectionReport,
    ExplanationResult,
    PolicyDecision,
    ResourceRecord,
    WorkspaceSnapshot,
)
from sws_agent.workflow import ActionPlanner

REPO_ROOT = Path(__file__).resolve().parent.parent

# Action-execution verbs that must never surface as MCP tool names.
ACTION_EXECUTION_NAMES = {
    "stop_resource",
    "start_resource",
    "execute_action",
    "execute",
    "run_action",
    "approve_and_execute",
}


def _now() -> datetime:
    return datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _make_snapshot(
    *,
    partial: bool = True,
    truncated: bool = False,
) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        snapshot_id="snap-1",
        created_at=_now(),
        regions=["us-east-1"],
        resource_types=[
            SWSResourceType.S3_BUCKET,
            SWSResourceType.LAMBDA_FUNCTION,
        ],
        resources=[
            ResourceRecord(
                resource_id="b-1",
                resource_type=SWSResourceType.S3_BUCKET,
                name="bucket-one",
            ),
            ResourceRecord(
                resource_id="fn-1",
                resource_type=SWSResourceType.LAMBDA_FUNCTION,
                name="function-one",
            ),
        ],
        counts={
            SWSResourceType.S3_BUCKET: 1,
            SWSResourceType.LAMBDA_FUNCTION: 1,
        },
        partial=partial,
        truncated=truncated,
    )


def _make_decisions() -> list[PolicyDecision]:
    return [
        PolicyDecision(
            resource_id="b-1",
            recommended_action=PotentialAction.LEAVE,
            risk_level=RiskLevel.NONE,
        ),
        PolicyDecision(
            resource_id="fn-1",
            recommended_action=PotentialAction.FLAG_FOR_REVIEW,
            risk_level=RiskLevel.LOW,
        ),
    ]


class SpyExplainer:
    """Records every explain call; never touches AWS or an LLM."""

    def __init__(self) -> None:
        self.calls: list[tuple[ResourceRecord, PolicyDecision]] = []

    def explain(
        self, resource: ResourceRecord, decision: PolicyDecision
    ) -> ExplanationResult:
        self.calls.append((resource.resource_id, decision.model_copy(deep=True)))
        return ExplanationResult(
            text=f"explained:{resource.resource_id}",
            claim_kind=ClaimKind.INTERPRETED,
            provider="spy",
            reason="deterministic",
        )


class SeededBackend:
    """SwsBackend fake wiring the seeded deterministic core into the server.

    Also records oracle state (collected snapshot, decision source, cost
    calls) so tests can assert what the adapters actually used.
    """

    def __init__(
        self,
        *,
        snapshot: WorkspaceSnapshot | None = None,
        decisions: list[PolicyDecision] | None = None,
        explainer: SpyExplainer | None = None,
        approval_store: InMemoryApprovalStore | None = None,
    ) -> None:
        self.snapshot = snapshot if snapshot is not None else _make_snapshot()
        self.decisions = decisions if decisions is not None else _make_decisions()
        self.explainer = explainer if explainer is not None else SpyExplainer()
        self.approval_store = (
            approval_store if approval_store is not None else InMemoryApprovalStore()
        )
        self.relationships: list = []
        self.cost_estimates: list = []
        self.collect_requests: list[dict] = []
        self.evaluate_requests: list[tuple[str, bool]] = []

    def collect_workspace(
        self,
        *,
        regions: list[str],
        limit: int | None = None,
        collect_cost: bool = False,
        cost_window_days: int | None = None,
        cost_group_by: list[str] | None = None,
        cost_end_date: object = None,
    ) -> WorkspaceSnapshot:
        self.collect_requests.append(
            {
                "regions": list(regions),
                "limit": limit,
                "collect_cost": collect_cost,
            }
        )
        return self.snapshot

    def derive_relationships(
        self, snapshot: WorkspaceSnapshot
    ) -> list:
        return self.relationships

    def evaluate_workspace(
        self,
        snapshot: WorkspaceSnapshot,
        relationships: object = None,
    ) -> list[PolicyDecision]:
        self.evaluate_requests.append((snapshot.snapshot_id, relationships is not None))
        return self.decisions

    def get_cost_estimates(
        self,
        *,
        end_date: object,
        window_days: int | None = None,
        group_by: list[str] | None = None,
    ) -> CostCollectionReport:
        return CostCollectionReport(estimates=self.cost_estimates)

    def explain(
        self, resource: ResourceRecord, decision: PolicyDecision
    ) -> ExplanationResult:
        return self.explainer.explain(resource, decision)

    def list_approvals(self) -> list:
        return list(self.approval_store.pending())

    def decide_ticket(
        self,
        ticket_id: str,
        *,
        decision: str,
        decided_by: str = "",
        reason: str = "",
    ) -> object:
        if decision == "grant":
            return self.approval_store.grant(
                ticket_id, decided_by=decided_by, reason=reason
            )
        return self.approval_store.deny(
            ticket_id, decided_by=decided_by, reason=reason
        )

    def request_approval(
        self,
        *,
        resource_id: str,
        resource_type: object,
        action: object,
        rationale: str = "",
    ) -> object:
        # Exercises the REAL M7 workflow through the fake backend seam.
        return ActionPlanner(
            approval_store=self.approval_store,
            execution_mode=ExecutionMode.SAFE,
        ).plan(
            resource_id=resource_id,
            resource_type=resource_type,
            action=action,
            rationale=rationale,
        )


def _payload(result: object) -> dict:
    """Extract the JSON result dict from an SDK CallToolResult content item."""
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


def _run(coro) -> object:
    return asyncio.run(coro)


@pytest.fixture
def seeded() -> SeededBackend:
    return SeededBackend()


@pytest.fixture
def server(seeded: SeededBackend) -> SwsMcpServer:
    return SwsMcpServer(backend=seeded)


@pytest.fixture
def snapshot_dict(seeded: SeededBackend) -> dict:
    return _jsonable(seeded.snapshot.model_dump())


# 1. Protocol conformance
def test_server_conforms_to_mcp_server_protocol(server: SwsMcpServer):
    assert callable(getattr(server, "add_tool", None))
    assert callable(getattr(server, "serve", None))
    # SwsMcpServer is a structural MCPServer and exposes the registry seam.
    assert isinstance(server.registry, ToolRegistry)


def test_backend_conforms_to_sws_backend_protocol(seeded: SeededBackend):
    for name in (
        "collect_workspace",
        "derive_relationships",
        "evaluate_workspace",
        "get_cost_estimates",
        "explain",
        "list_approvals",
        "decide_ticket",
        "request_approval",
    ):
        assert callable(getattr(seeded, name, None)), name
    # The protocol is importable and accepts the fake structurally.
    assert isinstance(seeded, SwsBackend)


# 2. All nine built-in tools registered
def test_all_nine_builtin_tools_registered(server: SwsMcpServer):
    assert len(server.registry.names()) == len(BUILTIN_TOOL_NAMES) == 9
    assert set(server.registry.names()) == set(BUILTIN_TOOL_NAMES)
    tools = _run(server.list_tools())
    assert len(tools) == 9
    assert {tool["name"] for tool in tools} == set(BUILTIN_TOOL_NAMES)


# 3. Deterministic tool names
def test_tool_names_are_deterministic(seeded: SeededBackend):
    first = SwsMcpServer(backend=SeededBackend(snapshot=_make_snapshot()))
    second = SwsMcpServer(backend=seeded)
    assert first.registry.names() == second.registry.names() == sorted(BUILTIN_TOOL_NAMES)
    # No duplicates: registration and SDK list agree exactly.
    names = first.registry.names()
    assert len(names) == len(set(names))


# 16. Deterministic metadata and schemas
def test_deterministic_metadata_and_schemas(seeded: SeededBackend):
    first = SwsMcpServer(backend=seeded)
    second = SwsMcpServer(backend=SeededBackend(snapshot=_make_snapshot()))
    meta_first = _run(first.list_tools())
    meta_second = _run(second.list_tools())
    assert meta_first == meta_second
    names_by_tool = {tool["name"]: tool for tool in meta_first}
    for name, tool in names_by_tool.items():
        assert name in BUILTIN_TOOL_NAMES
        assert tool["description"]
        assert tool["inputSchema"].get("type") == "object"
    assert SERVER_NAME in {SERVER_NAME, "sws"}
    assert SERVER_VERSION
    assert SERVER_DESCRIPTION
    # audit_workspace requires regions up front.
    audit_schema = names_by_tool["audit_workspace"]["inputSchema"]
    assert "regions" in audit_schema.get("required", [])


# 4. Schema/argument validation is deterministic
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("evaluate_workspace", {}),
        ("get_relationships", {}),
        ("explain_resource", {}),
        ("request_approval", {}),
    ],
)
def test_missing_or_unknown_arguments_fail_deterministically(
    server: SwsMcpServer, tool: str, arguments: dict
):
    with pytest.raises(ToolError):
        _run(server.call_tool(tool, arguments))


def test_invalid_snapshot_is_error_not_success(server: SwsMcpServer):
    with pytest.raises(ToolError) as exc:
        _run(server.call_tool("get_relationships", {"snapshot": {"junk": 1}}))
    assert "invalid snapshot" in str(exc.value)


def test_unknown_resource_is_error_not_success(
    server: SwsMcpServer, snapshot_dict: dict
):
    with pytest.raises(ToolError) as exc:
        _run(server.call_tool(
            "explain_resource",
            {"snapshot": snapshot_dict, "resource_id": "does-not-exist"},
        ))
    assert "resource not found" in str(exc.value)


def test_invalid_end_date_is_error_not_success(server: SwsMcpServer):
    with pytest.raises(ToolError) as exc:
        _run(server.call_tool("get_cost_estimates", {"end_date": "not-a-date"}))
    assert "ISO date" in str(exc.value)


# 5. Happy-path dispatch returns structured results
def test_evaluate_workspace_happy_path(server: SwsMcpServer, snapshot_dict: dict):
    result = _run(server.call_tool("evaluate_workspace", {"snapshot": snapshot_dict}))
    assert result.is_error is False
    decisions = _payload(result)["decisions"]
    assert [(d["resource_id"], d["recommended_action"]) for d in decisions] == [
        ("b-1", "leave"),
        ("fn-1", "flag_for_review"),
    ]


def test_collect_workspace_happy_path(server: SwsMcpServer):
    result = _run(server.call_tool(
        "collect_workspace", {"regions": ["us-east-1"], "limit": 100}
    ))
    assert result.is_error is False
    snapshot = _payload(result)["snapshot"]
    assert WorkspaceSnapshot.model_validate(snapshot).snapshot_id == "snap-1"


def test_get_relationships_happy_path(server: SwsMcpServer, snapshot_dict: dict):
    result = _run(server.call_tool("get_relationships", {"snapshot": snapshot_dict}))
    assert result.is_error is False
    assert _payload(result)["relationships"] == []


def test_get_cost_estimates_happy_path_with_seeded_backend(
    server: SwsMcpServer,
):
    result = _run(server.call_tool(
        "get_cost_estimates", {"end_date": "2026-01-02", "window_days": 30}
    ))
    assert result.is_error is False
    payload = _payload(result)
    assert payload["cost_estimates"] == []
    assert payload["truncated"] is False
    assert payload["failures"] == []


def test_decide_ticket_happy_path(server: SwsMcpServer):
    ticket = server._backend.approval_store.create_ticket(
        "b-1", PotentialAction.REQUEST_APPROVAL, rationale="review"
    )
    result = _run(server.call_tool(
        "decide_ticket",
        {"ticket_id": ticket.ticket_id, "decision": "grant", "decided_by": "tester"},
    ))
    assert result.is_error is False
    decided = _payload(result)["ticket"]
    assert decided["status"] == "granted"
    assert decided["ticket_id"] == ticket.ticket_id


def test_list_approvals_happy_path(server: SwsMcpServer):
    server._backend.approval_store.create_ticket(
        "fn-1", PotentialAction.REQUEST_APPROVAL, rationale="pending"
    )
    result = _run(server.call_tool("list_approvals", {}))
    assert result.is_error is False
    approvals = _payload(result)["approvals"]
    assert len(approvals) == 1
    assert approvals[0]["status"] == "pending"


# 7. audit_workspace deterministic semantics
def test_audit_workspace_preserves_snapshot_flags_and_envelope(
    server: SwsMcpServer, seeded: SeededBackend
):
    result = _run(server.call_tool(
        "audit_workspace",
        {"regions": ["us-east-1"], "collect_cost": True, "cost_end_date": "2026-01-02"},
    ))
    assert result.is_error is False
    payload = _payload(result)
    assert list(payload) == ["snapshot", "relationships", "decisions"]
    # Deterministic core is the single source: collection happened once.
    assert len(seeded.collect_requests) == 1
    assert seeded.collect_requests[0]["collect_cost"] is True
    snapshot = WorkspaceSnapshot.model_validate(payload["snapshot"])
    assert snapshot.partial is True
    assert snapshot.truncated is False
    assert len(snapshot.resources) == 2
    assert [(d["resource_id"], d["recommended_action"]) for d in payload["decisions"]] == [
        ("b-1", "leave"),
        ("fn-1", "flag_for_review"),
    ]


# 8/9/10. explain_resource resolves deterministic decision, explains exactly once
def test_explain_resource_resolves_deterministic_decision(
    server: SwsMcpServer, seeded: SeededBackend, snapshot_dict: dict
):
    result = _run(server.call_tool(
        "explain_resource", {"snapshot": snapshot_dict, "resource_id": "fn-1"}
    ))
    assert result.is_error is False
    payload = _payload(result)
    assert payload["decision"]["resource_id"] == "fn-1"
    assert payload["decision"]["recommended_action"] == "flag_for_review"
    explanation = payload["explanation"]
    assert explanation["provider"] == "spy"
    assert explanation["claim_kind"] == "interpreted"
    assert explanation["text"] == "explained:fn-1"


def test_explain_resource_invokes_explainer_exactly_once(
    server: SwsMcpServer, seeded: SeededBackend, snapshot_dict: dict
):
    _run(server.call_tool(
        "explain_resource", {"snapshot": snapshot_dict, "resource_id": "b-1"}
    ))
    assert len(seeded.explainer.calls) == 1
    called_resource_id, called_decision = seeded.explainer.calls[0]
    assert called_resource_id == "b-1"
    assert called_decision.recommended_action is PotentialAction.LEAVE


def test_explainer_cannot_mutate_deterministic_decision(
    server: SwsMcpServer, seeded: SeededBackend, snapshot_dict: dict
):
    before = [d.model_copy(deep=True) for d in seeded.decisions]
    result = _run(server.call_tool(
        "explain_resource", {"snapshot": snapshot_dict, "resource_id": "b-1"}
    ))
    payload = _payload(result)
    decision = PolicyDecision.model_validate(payload["decision"])
    assert decision == next(
        d for d in seeded.decisions if d.resource_id == "b-1"
    )
    # The explanation invoked the provider but never re-derived or changed it.
    assert seeded.decisions == before
    assert seeded.explainer.calls[0][1] == decision


# 11. No action-execution tool exposed
def test_no_action_execution_tool_exposed(server: SwsMcpServer):
    exposed = set(server.registry.names())
    assert exposed.isdisjoint(ACTION_EXECUTION_NAMES)
    for name in exposed:
        assert not name.startswith(("execute", "run_", "start_", "stop_"))


# 12. Null explanation provider is the default
def test_null_explanation_provider_is_default():
    backend = DefaultSwsBackend()
    resource = ResourceRecord(
        resource_id="b-1", resource_type=SWSResourceType.S3_BUCKET
    )
    decision = PolicyDecision(
        resource_id="b-1", recommended_action=PotentialAction.LEAVE
    )
    result = backend.explain(resource, decision)
    assert isinstance(backend._explainer, NullExplanationProvider)
    assert result.text is None
    assert result.provider == "null"
    assert result.claim_kind is ClaimKind.INTERPRETED


# 13. No AWS credentials / plumbing needed
def test_no_aws_credentials_needed(server: SwsMcpServer, snapshot_dict: dict):
    # Hermetic reads across the read-only tools with the seeded backend.
    _run(server.call_tool("audit_workspace", {"regions": ["us-east-1"]}))
    _run(server.call_tool("evaluate_workspace", {"snapshot": snapshot_dict}))
    _run(server.call_tool("explain_resource",
                          {"snapshot": snapshot_dict, "resource_id": "b-1"}))
    _run(server.call_tool("list_approvals", {}))
    # DefaultSwsBackend also constructs with no client and no AWS libraries.
    assert DefaultSwsBackend() is not None


def test_collect_tool_without_client_factory_fails_deterministically():
    server = SwsMcpServer()
    with pytest.raises(ToolError) as exc:
        _run(server.call_tool("collect_workspace", {"regions": ["us-east-1"]}))
    assert "no AWS client factory" in str(exc.value)


# 14. Importing the module never starts a listener
def test_import_does_not_start_listener():
    code = (
        "import sws_agent.mcp.server as s;"
        "srv = s.SwsMcpServer();"
        "app = srv.streamable_http_app();"
        "print('SWS_MCP_IMPORT_OK')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    assert "SWS_MCP_IMPORT_OK" in completed.stdout


# 15. ASGI app constructs without opening a port
def test_asgi_app_constructs_without_opening_port(server: SwsMcpServer):
    app = server.streamable_http_app(path="/mcp")
    assert callable(app)
    # Startup is explicit only: run()/main() exist and are not invoked here.
    assert callable(getattr(server, "run", None))


# Wire-result round-trip through the deterministic models
def test_output_snapshot_round_trips_through_model_validation(
    server: SwsMcpServer,
):
    payload = _payload(_run(server.call_tool(
        "audit_workspace", {"regions": ["us-east-1"]}
    )))
    rebuilt = WorkspaceSnapshot.model_validate(payload["snapshot"])
    assert rebuilt.counts[SWSResourceType.S3_BUCKET] == 1
    assert rebuilt.partial is True


# Registry seam dispatches built-in tools like any other handler
def test_registry_seam_dispatches_builtin_tools(
    server: SwsMcpServer, seeded: SeededBackend, snapshot_dict: dict
):
    payload = server.registry.invoke(
        "evaluate_workspace", {"snapshot": snapshot_dict}
    )
    assert [(d["resource_id"], d["recommended_action"]) for d in payload["decisions"]] == [
        ("b-1", "leave"),
        ("fn-1", "flag_for_review"),
    ]


# Unknown-tool lookup is deterministic at the SDK boundary too
def test_unknown_tool_raises_tool_error(server: SwsMcpServer):
    with pytest.raises(ToolError):
        _run(server.call_tool("not_a_real_tool", {}))


# 17. M7 request_approval: the pre-execution authorization workflow tool.
def test_request_approval_pending_action_creates_ticket(
    server: SwsMcpServer, seeded: SeededBackend
):
    result = _run(server.call_tool(
        "request_approval",
        {
            "resource_id": "fn-1",
            "resource_type": "lambda_function",
            "action": "stop_resource",
            "rationale": "candidate for review",
        },
    ))
    assert result.is_error is False
    plan = _payload(result)["plan"]
    assert plan["resource_id"] == "fn-1"
    assert plan["action"] == "stop_resource"
    assert plan["execution_mode"] == "safe"
    assert plan["authorization"]["decision"] == "pending_approval"
    assert plan["authorization"]["requires_human_approval"] is True
    assert plan["executed"] is False
    assert plan["ticket"]["status"] == "pending"
    assert plan["ticket"]["rationale"] == "candidate for review"
    tickets = seeded.approval_store.pending()
    assert [t.ticket_id for t in tickets] == [plan["ticket"]["ticket_id"]]


def test_request_approval_zero_side_effect_action_no_ticket(
    server: SwsMcpServer, seeded: SeededBackend
):
    result = _run(server.call_tool(
        "request_approval",
        {"resource_id": "b-1", "resource_type": "s3_bucket", "action": "leave"},
    ))
    assert result.is_error is False
    plan = _payload(result)["plan"]
    assert plan["authorization"]["decision"] == "authorized"
    assert plan["ticket"] is None
    assert plan["executed"] is False
    assert seeded.approval_store.pending() == []


def test_request_approval_invalid_action_is_error(server: SwsMcpServer):
    with pytest.raises(ToolError) as exc:
        _run(server.call_tool(
            "request_approval",
            {"resource_id": "b-1", "resource_type": "s3_bucket", "action": "run_amok"},
        ))
    assert "validation" in str(exc.value).lower()


def test_request_approval_invalid_resource_type_is_error(server: SwsMcpServer):
    with pytest.raises(ToolError) as exc:
        _run(server.call_tool(
            "request_approval",
            {"resource_id": "b-1", "resource_type": "not_a_type", "action": "leave"},
        ))
    assert "validation" in str(exc.value).lower()


# 18. M7 honesty add-on: the standalone cost tool surfaces truncation.
def test_get_cost_estimates_surfaces_truncation_via_trace():
    class TruncatingClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_cost_and_usage(self, **kwargs):
            self.calls += 1
            return {
                "ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": "1.00"}}}],
                "NextPageToken": "more",
            }

    backend = DefaultSwsBackend(client_factory=lambda: TruncatingClient())
    report = backend.get_cost_estimates(end_date=date(2026, 2, 1), window_days=7)
    assert report.truncated is True
    assert report.estimates
    assert report.failures == []


def test_get_cost_estimates_surfaces_primary_failure():
    class FailingClient:
        def get_cost_and_usage(self, **kwargs):
            raise RuntimeError("cost service unavailable")

    backend = DefaultSwsBackend(client_factory=lambda: FailingClient())
    report = backend.get_cost_estimates(end_date=date(2026, 2, 1), window_days=7)
    assert report.estimates == []
    assert len(report.failures) == 1
    assert report.failures[0].category == "primary"
    assert report.failures[0].fatal is True


# --- M12 Phase 3A: durable approval-store error boundary ---


class FailingApprovalStore:
    """Approval store whose every operation fails with a given error.

    Structural stand-in for ``DurableApprovalStore``: it exposes the store
    surface the backend uses and refuses all of it, so tests can assert the
    MCP boundary fails closed without constructing a real database.
    """

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[str] = []

    def _refuse(self, op: str):
        self.calls.append(op)
        raise self.exc

    def pending(self):
        return self._refuse("pending")

    def get(self, ticket_id: str):
        return self._refuse("get")

    def create_ticket(self, *args, **kwargs):
        return self._refuse("create_ticket")

    def grant(self, ticket_id: str, **kwargs):
        return self._refuse("grant")

    def deny(self, ticket_id: str, **kwargs):
        return self._refuse("deny")

    def consume(self, ticket_id: str, **kwargs):
        return self._refuse("consume")


@pytest.mark.parametrize(
    "exc",
    [
        ApprovalStoreCorruptionError("approval ledger failed integrity check"),
        ApprovalStoreUnavailableError("approval ledger is locked"),
    ],
    ids=["corruption", "unavailable"],
)
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("list_approvals", {}),
        (
            "request_approval",
            {
                "resource_id": "fn-1",
                "resource_type": "lambda_function",
                "action": "stop_resource",
                "rationale": "deploy window",
            },
        ),
        ("decide_ticket", {"ticket_id": "t1", "decision": "grant"}),
        ("decide_ticket", {"ticket_id": "t1", "decision": "deny"}),
    ],
    ids=["list", "request", "grant", "deny"],
)
def test_durable_store_failures_surface_as_tool_error(exc, tool, args):
    """A corrupt/unavailable ledger is an error, never a silent empty list.

    Each approval tool is exercised against a store that refuses everything.
    The boundary must translate the failure into ``ToolError`` so the caller
    knows the approval state is unknown, rather than returning an empty
    pending list or a fabricated ticket that looks like success.
    """
    store = FailingApprovalStore(exc)
    backend = DefaultSwsBackend(approval_store=store)
    server = SwsMcpServer(backend=backend)

    with pytest.raises(ToolError) as caught:
        _run(server.call_tool(tool, args))

    assert str(exc) in str(caught.value)
    assert store.calls, "the store was never consulted"


def test_unknown_ticket_behavior_is_preserved():
    """The pre-existing UnknownTicketError mapping still holds."""
    backend = DefaultSwsBackend(approval_store=InMemoryApprovalStore())
    server = SwsMcpServer(backend=backend)

    with pytest.raises(ToolError, match="no such ticket|not found|unknown"):
        _run(server.call_tool("decide_ticket", {"ticket_id": "missing", "decision": "grant"}))


def test_no_fallback_to_in_memory_when_durable_store_fails():
    """Fail-closed means no second approval authority appears.

    The injected store is the only authority consulted; it is not swapped for
    an ``InMemoryApprovalStore`` on failure, and no tool succeeds.
    """
    store = FailingApprovalStore(
        ApprovalStoreCorruptionError("approval ledger failed integrity check")
    )
    server = SwsMcpServer(backend=DefaultSwsBackend(approval_store=store))

    for tool, args in (
        ("list_approvals", {}),
        ("decide_ticket", {"ticket_id": "t1", "decision": "grant"}),
    ):
        with pytest.raises(ToolError):
            _run(server.call_tool(tool, args))

    assert "pending" in store.calls
    # Only the injected (failing) store was ever touched.
    assert isinstance(server._backend._approval_store, FailingApprovalStore)


# --- M12 Phase 3A: the runtime migration must NOT have happened ---


def test_default_backend_still_uses_in_memory_store():
    """Production default is unchanged: no store injected means in-memory."""
    backend = DefaultSwsBackend()
    assert isinstance(backend._approval_store, InMemoryApprovalStore)
    assert backend._planner._store is backend._approval_store


def test_approval_db_env_is_not_consulted_by_the_backend_class(monkeypatch):
    """Store selection is the composition root's job, not the backend's.

    ``DefaultSwsBackend()`` on its own always uses the in-memory default even
    when ``SWS_APPROVAL_DB`` is set. Only the composition root consults the
    variable, so constructing the backend elsewhere can never silently pick
    up a durable ledger.
    """
    monkeypatch.setenv(
        SWS_APPROVAL_DB_ENV, str(Path("D:/sws-test/approvals.sqlite3"))
    )

    backend = DefaultSwsBackend()
    assert isinstance(backend._approval_store, InMemoryApprovalStore)


def test_phase_3a_did_not_add_tools():
    """The surface is unchanged: still exactly nine tools."""
    backend = DefaultSwsBackend(
        approval_store=FailingApprovalStore(
            ApprovalStoreUnavailableError("approval ledger is locked")
        )
    )
    server = SwsMcpServer(backend=backend)
    assert len(server.registry.names()) == len(BUILTIN_TOOL_NAMES) == 9
    assert set(server.registry.names()) == set(BUILTIN_TOOL_NAMES)
    assert len(_run(server.list_tools())) == 9