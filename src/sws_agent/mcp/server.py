"""Real MCP Streamable HTTP server (M4) exposing the deterministic SWS core.

MCP is transport/exposure only. Inventory, relationships, policy, cost,
authorization, and explanation logic all live in ``sws_agent`` modules; this
module adapts their public functions into MCP tools with thin, typed
handlers. No SWS business logic is recreated here and no action-execution
tool is exposed.

Design contract:

  - Built on the MCP SDK v2 (Streamable HTTP transport). The server exposes
    the SDK's ASGI application factory (``streamable_http_app()``); nothing
    here implements JSON-RPC or MCP protocol by hand.
  - Importing or constructing this module never opens a network listener.
    ``streamable_http_app()`` builds the ASGI application on demand and
    ``run()`` / ``main()`` (the ``__main__`` entry point) start it only when
    invoked explicitly.
  - ``ToolRegistry`` (``sws_agent.mcp``) remains the logical registry
    boundary: every tool is registered there with a deterministic name and
    surfaced through the SDK server.
  - Handlers are stateless. The domain is deterministic and replayable; no
    persistent sessions and no database. Stateful workspace data travels
    with the client: tools that need a snapshot accept the structured
    snapshot returned by ``collect_workspace`` or ``audit_workspace``.
  - Optional durable audit ledger (M8): when an ``audit_store`` is injected
    (or ``SWS_AUDIT_DIR`` is set for ``main()``), the backend writes an
    append-only JSONL ledger covering runs, snapshots, decisions, plans,
    tickets, explanations, and cost collection. Persistence is a side
    effect: tool request/response shapes are unchanged, and a persistence
    failure surfaces as a ``ToolError`` (never a silent drop).
  - Distrusted input fails deterministically: schema validation is delegated
    to the SDK, and adapter-level domain errors (unknown resource, invalid
    decision, malformed snapshot/date) are raised as ``ToolError`` so they
    surface as ``isError`` results, never as fabricated success.
  - Explanations are resolved from the deterministic snapshot and policy
    evaluation only; the LLM is never asked to re-derive a decision.

The eventual AgentCore Harness consumes this server through its remote MCP
integration; authentication and deployment are out of scope for the transport
layer and documented for that later step.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import enum
from functools import wraps
from typing import Any, Callable, Protocol, runtime_checkable
from uuid import uuid4

from mcp.server.mcpserver import MCPServer as SdkMCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from ..approval import ApprovalError, InMemoryApprovalStore, UnknownTicketError
from ..audit import (
    AuditRecordKind,
    AuditStoreError,
    JsonlAuditStore,
    LEDGER_FILENAME,
    cost_payload,
    decision_payload,
    explanation_payload,
    plan_payload,
    run_payload,
    snapshot_payload,
    ticket_payload,
)
from ..aws import AwsClientFactory, aws_config_from_env
from ..config import audit_dir_from_env, execution_mode_from_env
from ..constants import (
    ExecutionMode,
    PotentialAction,
    SWSResourceType,
    TraceEventType,
    TraceStatus,
)
from ..cost_explorer import CostExplorerCollector
from ..explanation import NullExplanationProvider
from ..models import (
    ActionPlan,
    ApprovalTicket,
    CostCollectionReport,
    CostEstimate,
    ExplanationResult,
    PolicyDecision,
    ResourceRecord,
    WorkspaceSnapshot,
)
from ..policy import evaluate_workspace
from ..relationships import WorkspaceSnapshotTooLargeError, derive_relationships
from ..trace import TraceRecorder
from ..workflow import ActionPlanner
from ..workspace import (
    _failure_from_event,
    collect_workspace as _collect_workspace,
)
from . import ToolHandler, ToolRegistry

SERVER_NAME: str = "sws"
SERVER_VERSION: str = "0.1.0"
SERVER_DESCRIPTION: str = (
    "SWS (Semantic Workspace Steward): deterministic AWS workspace inventory, "
    "relationships, policy, cost, and explanation over MCP."
)

# Tools are intentionally limited to read-only analysis, the approval
# ticket lifecycle, and the pre-execution authorization workflow. No
# action-execution (e.g. STOP_RESOURCE) tool is exposed.
BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "audit_workspace",
    "collect_workspace",
    "get_relationships",
    "evaluate_workspace",
    "get_cost_estimates",
    "explain_resource",
    "list_approvals",
    "decide_ticket",
    "request_approval",
)


@runtime_checkable
class SwsBackend(Protocol):
    """Thin seam between MCP tools and the deterministic SWS core.

    Implementations decide where state comes from (AWS clients in production,
    seeded snapshots in hermetic tests) so the MCP layer never touches AWS
    plumbing and never reimplements domain logic.
    """

    def collect_workspace(
        self,
        *,
        regions: list[str],
        limit: int | None = None,
        collect_cost: bool = False,
        cost_window_days: int | None = None,
        cost_group_by: list[str] | None = None,
        cost_end_date: date | None = None,
    ) -> WorkspaceSnapshot: ...

    def derive_relationships(
        self, snapshot: WorkspaceSnapshot
    ) -> list[Any]: ...

    def evaluate_workspace(
        self,
        snapshot: WorkspaceSnapshot,
        relationships: list[Any] | None = None,
    ) -> list[PolicyDecision]: ...

    def get_cost_estimates(
        self,
        *,
        end_date: date,
        window_days: int | None = None,
        group_by: list[str] | None = None,
    ) -> CostCollectionReport: ...

    def explain(
        self, resource: ResourceRecord, decision: PolicyDecision
    ) -> ExplanationResult: ...

    def list_approvals(self) -> list[ApprovalTicket]: ...

    def decide_ticket(
        self,
        ticket_id: str,
        *,
        decision: str,
        decided_by: str = "",
        reason: str = "",
    ) -> ApprovalTicket: ...

    def request_approval(
        self,
        *,
        resource_id: str,
        resource_type: SWSResourceType,
        action: PotentialAction,
        rationale: str = "",
    ) -> ActionPlan: ...


class DefaultSwsBackend:
    """Production backend: real SWS modules, injected AWS client factory.

    Constructible without any client factory, so hermetic tests and any
    no-LLM/no-AWS deployment keep working. The AWS client is built lazily and
    only when an inventory/cost tool actually runs.
    """

    def __init__(
        self,
        client_factory: Callable[[], Any] | None = None,
        *,
        explainer: Any | None = None,
        approval_store: Any | None = None,
        execution_mode: ExecutionMode = ExecutionMode.SAFE,
        audit_store: Any | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._explainer = explainer if explainer is not None else NullExplanationProvider()
        self._approval_store = (
            approval_store if approval_store is not None else InMemoryApprovalStore()
        )
        # M8: the optional durable audit store. ``None`` (the default) keeps
        # the backend fully hermetic; when set, every tool writes its
        # append-only ledger records after a successful, deterministic result.
        self._audit_store = audit_store
        # M7: the authorization gate is now reachable through the backend.
        # The execution mode is fixed at construction (an operator setting);
        # it is never a per-request client input.
        self._planner = ActionPlanner(
            approval_store=self._approval_store,
            execution_mode=execution_mode,
        )

    def _client(self) -> Any:
        if self._client_factory is None:
            raise ToolError(
                "no AWS client factory configured for this server"
            )
        return self._client_factory()

    def _audit(
        self,
        kind: AuditRecordKind,
        *,
        run_id: str | None = None,
        snapshot_id: str | None = None,
        decision_id: str | None = None,
        action_plan_id: str | None = None,
        ticket_id: str | None = None,
        resource_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Write one ledger record when an audit store is configured."""
        audit = self._audit_store
        if audit is None:
            return
        audit.write(
            kind,
            run_id=run_id,
            snapshot_id=snapshot_id,
            decision_id=decision_id,
            action_plan_id=action_plan_id,
            ticket_id=ticket_id,
            resource_id=resource_id,
            payload=payload,
        )

    def collect_workspace(
        self,
        *,
        regions: list[str],
        limit: int | None = None,
        collect_cost: bool = False,
        cost_window_days: int | None = None,
        cost_group_by: list[str] | None = None,
        cost_end_date: date | None = None,
    ) -> WorkspaceSnapshot:
        run_id = uuid4().hex
        recorder = TraceRecorder()
        snapshot = _collect_workspace(
            client=self._client(),
            regions=regions,
            limit=limit,
            run_id=run_id,
            trace=recorder,
            collect_cost=collect_cost,
            cost_window_days=cost_window_days,
            cost_group_by=cost_group_by,
            cost_end_date=cost_end_date,
        )
        self._audit(
            AuditRecordKind.RUN,
            run_id=run_id,
            snapshot_id=snapshot.snapshot_id,
            payload=run_payload("workspace", recorder.to_dicts()),
        )
        self._audit(
            AuditRecordKind.SNAPSHOT,
            run_id=run_id,
            snapshot_id=snapshot.snapshot_id,
            payload=snapshot_payload(snapshot),
        )
        return snapshot

    def derive_relationships(
        self, snapshot: WorkspaceSnapshot
    ) -> list[Any]:
        return derive_relationships(snapshot)

    def evaluate_workspace(
        self,
        snapshot: WorkspaceSnapshot,
        relationships: list[Any] | None = None,
    ) -> list[PolicyDecision]:
        decisions = evaluate_workspace(snapshot, relationships=relationships)
        for decision in decisions:
            self._audit(
                AuditRecordKind.DECISION,
                run_id=decision.run_id or snapshot.run_id,
                snapshot_id=decision.snapshot_id or snapshot.snapshot_id,
                decision_id=decision.decision_id,
                resource_id=decision.resource_id,
                payload=decision_payload(decision),
            )
        return decisions

    def get_cost_estimates(
        self,
        *,
        end_date: date,
        window_days: int | None = None,
        group_by: list[str] | None = None,
    ) -> CostCollectionReport:
        # M7 honesty add-on: the standalone cost tool now runs behind a
        # fresh trace so truncation and failures are surfaced instead of
        # silently dropped (matching the audit_workspace cost contract).
        run_id = uuid4().hex
        recorder = TraceRecorder()
        estimates = CostExplorerCollector(self._client(), trace=recorder).collect(
            window_days=window_days,
            end_date=end_date,
            group_by=group_by,
        )
        report = _cost_report_from_trace(estimates, list(recorder))
        self._audit(
            AuditRecordKind.RUN,
            run_id=run_id,
            payload=run_payload("cost", recorder.to_dicts()),
        )
        self._audit(
            AuditRecordKind.COST,
            run_id=run_id,
            payload=cost_payload(
                end_date=end_date.isoformat(),
                window_days=window_days,
                group_by=group_by,
                report=report,
            ),
        )
        return report

    def explain(
        self, resource: ResourceRecord, decision: PolicyDecision
    ) -> ExplanationResult:
        result = self._explainer.explain(resource, decision)
        self._audit(
            AuditRecordKind.EXPLANATION,
            run_id=decision.run_id,
            snapshot_id=decision.snapshot_id,
            decision_id=decision.decision_id,
            resource_id=resource.resource_id,
            payload=explanation_payload(
                resource_id=resource.resource_id,
                decision_id=decision.decision_id,
                snapshot_id=decision.snapshot_id,
                run_id=decision.run_id,
                provider=result.provider,
                claim_kind=result.claim_kind.value,
                reason=result.reason,
            ),
        )
        return result

    def list_approvals(self) -> list[ApprovalTicket]:
        return list(self._approval_store.pending())

    def decide_ticket(
        self,
        ticket_id: str,
        *,
        decision: str,
        decided_by: str = "",
        reason: str = "",
    ) -> ApprovalTicket:
        if decision == "grant":
            ticket = self._approval_store.grant(
                ticket_id, decided_by=decided_by, reason=reason
            )
        elif decision == "deny":
            ticket = self._approval_store.deny(
                ticket_id, decided_by=decided_by, reason=reason
            )
        else:
            raise ToolError("decision must be 'grant' or 'deny'")
        self._audit(
            AuditRecordKind.TICKET,
            run_id=None,
            action_plan_id=ticket.plan_id,
            ticket_id=ticket.ticket_id,
            resource_id=ticket.resource_id,
            payload=ticket_payload(ticket),
        )
        return ticket

    def request_approval(
        self,
        *,
        resource_id: str,
        resource_type: SWSResourceType,
        action: PotentialAction,
        rationale: str = "",
    ) -> ActionPlan:
        plan = self._planner.plan(
            resource_id=resource_id,
            resource_type=resource_type,
            action=action,
            rationale=rationale,
        )
        self._audit(
            AuditRecordKind.PLAN,
            run_id=None,
            action_plan_id=plan.action_plan_id,
            resource_id=plan.resource_id,
            payload=plan_payload(plan),
        )
        if plan.ticket is not None:
            self._audit(
                AuditRecordKind.TICKET,
                run_id=None,
                action_plan_id=plan.action_plan_id,
                ticket_id=plan.ticket.ticket_id,
                resource_id=plan.ticket.resource_id,
                payload=ticket_payload(plan.ticket),
            )
        return plan


def _jsonable(value: Any) -> Any:
    """Convert domain values into JSON/structuredContent-safe values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump())
    if isinstance(value, dict):
        return {_jsonable(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _guarded(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Convert deterministic domain failures into MCP ToolError results."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (
            ValueError,
            TypeError,
            WorkspaceSnapshotTooLargeError,
            ApprovalError,
            UnknownTicketError,
            AuditStoreError,
        ) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _date_optional(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError("end_date must be an ISO date (YYYY-MM-DD)") from None


def _snapshot_from(value: Any) -> WorkspaceSnapshot:
    """Rehydrate a structured snapshot with a deterministic failure."""
    try:
        return WorkspaceSnapshot.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        raise ToolError(
            "invalid snapshot: expected the structured snapshot returned by "
            "collect_workspace or audit_workspace"
        ) from None


def _cost_report_from_trace(
    estimates: list[CostEstimate], events: list[Any]
) -> CostCollectionReport:
    """Derive the honest completeness summary from a cost run's trace.

    Mirrors the workspace snapshot contract for cost data (M2C-B/M2C-E):
    ``truncated`` is True only when a SUCCEEDED INVENTORY_QUERY event carries
    genuine "more data existed" metadata, and ``failures`` maps every FAILED
    INVENTORY_QUERY event 1:1 via the same workspace failure builder.
    """
    inventory_events = [
        event
        for event in events
        if event.event_type is TraceEventType.INVENTORY_QUERY
    ]
    succeeded_types: set[SWSResourceType] = set()
    for event in inventory_events:
        if event.status is not TraceStatus.SUCCEEDED:
            continue
        try:
            succeeded_types.add(
                SWSResourceType((event.metadata or {}).get("resource_type"))
            )
        except (TypeError, ValueError):
            continue
    truncated = any(
        event.status is TraceStatus.SUCCEEDED
        and (event.metadata or {}).get("truncated") is True
        for event in inventory_events
    )
    failures = [
        failure
        for event in inventory_events
        if event.status is TraceStatus.FAILED
        for failure in [_failure_from_event(event, succeeded_types=succeeded_types)]
        if failure is not None
    ]
    return CostCollectionReport(
        estimates=estimates, truncated=truncated, failures=failures
    )


def _adapter_functions(
    backend: SwsBackend,
) -> list[tuple[str, str, Callable[..., dict[str, Any]]]]:
    """The nine built-in tools: (name, description, typed handler)."""

    def collect_workspace(
        *,
        regions: list[str],
        limit: int | None = None,
        collect_cost: bool = False,
        cost_window_days: int | None = None,
        cost_group_by: list[str] | None = None,
        cost_end_date: str | None = None,
    ) -> dict[str, Any]:
        snapshot = backend.collect_workspace(
            regions=regions,
            limit=limit,
            collect_cost=collect_cost,
            cost_window_days=cost_window_days,
            cost_group_by=cost_group_by,
            cost_end_date=_date_optional(cost_end_date),
        )
        return {"snapshot": _jsonable(snapshot.model_dump())}

    def get_relationships(
        *, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        relationship_list = backend.derive_relationships(_snapshot_from(snapshot))
        return {
            "relationships": [
                _jsonable(relationship.model_dump())
                for relationship in relationship_list
            ]
        }

    def evaluate_workspace_tool(
        *,
        snapshot: dict[str, Any],
        include_relationships: bool = True,
    ) -> dict[str, Any]:
        workspace = _snapshot_from(snapshot)
        relationships = (
            backend.derive_relationships(workspace)
            if include_relationships
            else None
        )
        decisions = backend.evaluate_workspace(workspace, relationships)
        return {"decisions": [_jsonable(d.model_dump()) for d in decisions]}

    def audit_workspace(
        *,
        regions: list[str],
        limit: int | None = None,
        collect_cost: bool = False,
        cost_window_days: int | None = None,
        cost_group_by: list[str] | None = None,
        cost_end_date: str | None = None,
    ) -> dict[str, Any]:
        snapshot = backend.collect_workspace(
            regions=regions,
            limit=limit,
            collect_cost=collect_cost,
            cost_window_days=cost_window_days,
            cost_group_by=cost_group_by,
            cost_end_date=_date_optional(cost_end_date),
        )
        relationships = backend.derive_relationships(snapshot)
        decisions = backend.evaluate_workspace(snapshot, relationships)
        return {
            "snapshot": _jsonable(snapshot.model_dump()),
            "relationships": [
                _jsonable(r.model_dump()) for r in relationships
            ],
            "decisions": [_jsonable(d.model_dump()) for d in decisions],
        }

    def get_cost_estimates(
        *,
        end_date: str,
        window_days: int | None = None,
        group_by: list[str] | None = None,
    ) -> dict[str, Any]:
        parsed_end_date = _date_optional(end_date)
        if parsed_end_date is None:
            raise ToolError("end_date is required and must be an ISO date (YYYY-MM-DD)")
        report = backend.get_cost_estimates(
            end_date=parsed_end_date,
            window_days=window_days,
            group_by=group_by,
        )
        return {
            "cost_estimates": [
                _jsonable(e.model_dump()) for e in report.estimates
            ],
            "truncated": report.truncated,
            "failures": [
                _jsonable(f.model_dump()) for f in report.failures
            ],
        }

    def explain_resource(
        *, snapshot: dict[str, Any], resource_id: str
    ) -> dict[str, Any]:
        workspace = _snapshot_from(snapshot)
        resource = next(
            (r for r in workspace.resources if r.resource_id == resource_id),
            None,
        )
        if resource is None:
            raise ToolError(
                f"resource not found in snapshot: {resource_id}"
            )
        decision = next(
            (
                d
                for d in backend.evaluate_workspace(workspace, None)
                if d.resource_id == resource_id
            ),
            None,
        )
        if decision is None:
            raise ToolError(
                f"no deterministic policy decision for resource: {resource_id}"
            )
        explanation = backend.explain(resource, decision)
        return {
            "explanation": _jsonable(explanation.model_dump()),
            "decision": _jsonable(decision.model_dump()),
        }

    def list_approvals() -> dict[str, Any]:
        return {
            "approvals": [
                _jsonable(t.model_dump()) for t in backend.list_approvals()
            ]
        }

    def decide_ticket(
        *,
        ticket_id: str,
        decision: str,
        decided_by: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        ticket = backend.decide_ticket(
            ticket_id,
            decision=decision,
            decided_by=decided_by,
            reason=reason,
        )
        return {"ticket": _jsonable(ticket.model_dump())}

    def request_approval(
        *,
        resource_id: str,
        resource_type: str,
        action: str,
        rationale: str = "",
    ) -> dict[str, Any]:
        plan = backend.request_approval(
            resource_id=resource_id,
            resource_type=resource_type,
            action=action,
            rationale=rationale,
        )
        return {"plan": _jsonable(plan.model_dump())}

    tools: list[tuple[str, str, Callable[..., dict[str, Any]]]] = [
        (
            "audit_workspace",
            (
                "Collect a workspace snapshot, derive relationships, evaluate "
                "deterministic policy, and return the full audit envelope "
                "(snapshot with completeness flags and failures, relationships, "
                "decisions) in one call."
            ),
            _guarded(audit_workspace),
        ),
        (
            "collect_workspace",
            (
                "Collect a deterministic workspace snapshot (inventory, "
                "optional cost) as structured data. The returned snapshot can "
                "be passed back to get_relationships, evaluate_workspace, and "
                "explain_resource."
            ),
            _guarded(collect_workspace),
        ),
        (
            "get_relationships",
            (
                "Derive deterministic resource relationships (same account, "
                "same region, same owner tag) from a structured snapshot."
            ),
            _guarded(get_relationships),
        ),
        (
            "evaluate_workspace",
            (
                "Evaluate deterministic policy for every resource in a "
                "structured snapshot. Completeness flags come from the "
                "snapshot; the LLM is never involved."
            ),
            _guarded(evaluate_workspace_tool),
        ),
        (
            "get_cost_estimates",
            (
                "Query account-level cost estimates for a window ending at "
                "end_date (ISO YYYY-MM-DD). Cost figures are projected "
                "estimates with stated assumptions, never an authorization "
                "gate."
            ),
            _guarded(get_cost_estimates),
        ),
        (
            "explain_resource",
            (
                "Resolve one resource and its deterministic policy decision "
                "from a structured snapshot and invoke the configured "
                "explanation provider exactly once. Explanations are "
                "INTERPRETED claims and can never change the decision."
            ),
            _guarded(explain_resource),
        ),
        (
            "list_approvals",
            (
                "List pending human-approval tickets from the in-memory "
                "approval store."
            ),
            _guarded(list_approvals),
        ),
        (
            "decide_ticket",
            (
                "Grant or deny a pending approval ticket by id. Only affects "
                "the approval store; never executes an AWS action."
            ),
            _guarded(decide_ticket),
        ),
        (
            "request_approval",
            (
                "Plan one candidate action against the deterministic "
                "authorization gate for the server's configured execution "
                "mode. Returns the authorization decision and, when human "
                "approval is required, creates a PENDING approval ticket. "
                "Nothing is executed."
            ),
            _guarded(request_approval),
        ),
    ]
    return tools


class SwsMcpServer:
    """Concrete MCP Streamable HTTP server over the deterministic SWS core.

    Implements the ``sws_agent.mcp.MCPServer`` protocol. ``ToolRegistry`` is
    the logical registry; every builtin tool is registered there and surfaced
    through the SDK v2 server. Constructing this class never opens a network
    port.
    """

    def __init__(
        self,
        *,
        name: str = SERVER_NAME,
        version: str = SERVER_VERSION,
        description: str = SERVER_DESCRIPTION,
        backend: SwsBackend | None = None,
    ) -> None:
        self._name = name
        self._version = version
        self._description = description
        self._backend = backend if backend is not None else DefaultSwsBackend()
        self.registry: ToolRegistry = ToolRegistry()
        self._sdk = SdkMCPServer(
            name=name, version=version, description=description
        )
        for tool_name, description, handler in _adapter_functions(self._backend):
            self.registry.register(
                tool_name, lambda arguments, _handler=handler: _handler(**arguments)
            )
            self._sdk.add_tool(
                handler, name=tool_name, description=description
            )

    def tool_names(self) -> list[str]:
        """Deterministic (sorted) names of the exposed tools."""
        return self.registry.names()

    async def list_tools(self) -> list[dict[str, Any]]:
        """Tool metadata as surfaced by the SDK (name/description/inputSchema)."""
        tools = await self._sdk.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in tools
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Dispatch one tool call through the SDK server (used by tests)."""
        return await self._sdk.call_tool(name, arguments or {}, context=None)

    def streamable_http_app(self, *, path: str = "/mcp"):
        """Build the SDK Streamable HTTP ASGI application (no port opened)."""
        return self._sdk.streamable_http_app(streamable_http_path=path)

    def run(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        path: str = "/mcp",
    ) -> None:
        """Serve the Streamable HTTP application explicitly (blocking)."""
        import asyncio

        async def _serve() -> None:
            await self._sdk.run_streamable_http_async(
                host=host, port=port, streamable_http_path=path
            )

        asyncio.run(_serve())

    # --- sws_agent.mcp.MCPServer protocol ---
    def add_tool(self, name: str, handler: ToolHandler) -> None:
        """Registry a caller-supplied dict->dict tool on the protocol seam."""
        self.registry.register(name, handler)

        def extended(arguments: dict[str, Any]) -> dict[str, Any]:
            return handler(arguments)

        self._sdk.add_tool(
            extended,
            name=name,
            description=f"SWS extension tool {name}",
        )

    def serve(self, *, transport: str = "streamable-http") -> None:
        """Protocol entry point; startup stays explicit via ``run``/``main``."""
        if transport != "streamable-http":
            raise ValueError(
                f"unsupported transport: {transport!r}; SWS targets "
                "streamable-http"
            )
        raise ValueError(
            "serve() only validates the transport; start the server "
            "explicitly with run(host=..., port=..., path=...) or "
            "python -m sws_agent.mcp.server"
        )


def main(argv: list[str] | None = None) -> int:
    """Explicit CLI entry point; binds loopback by default.

    Real-AWS mode is opt-in: when ``SWS_AWS_REGION`` and/or
    ``SWS_AWS_PROFILE`` are set, the server is constructed with a
    ``DefaultSwsBackend`` whose client factory builds real boto3 clients
    lazily, at tool execution time. Without them the server stays fully
    hermetic (the existing no-factory deterministic ``ToolError``). No AWS
    calls and no boto3 import happen at startup or on import either way.

    ``SWS_EXECUTION_MODE`` (safe/review/autonomous) sets the operator-level
    execution mode consumed by the ``request_approval`` authorization gate
    (M7); absent or invalid-free, it validates via ``SWSRuntimeConfig`` and
    fails fast on unknown values, defaulting to ``safe``.

    ``SWS_AUDIT_DIR`` (M8) names a directory for the append-only JSONL audit
    ledger. When set, the server persists every tool's ledger records there
    and fails fast at startup on an unreadable/corrupt ledger; when absent,
    the server runs fully hermetic with no ledger writes.
    """
    parser = argparse.ArgumentParser(prog="sws-mcp-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/mcp")
    args = parser.parse_args(argv)
    config = aws_config_from_env()
    execution_mode = execution_mode_from_env()
    audit_dir = audit_dir_from_env()
    audit_store = (
        JsonlAuditStore(audit_dir / LEDGER_FILENAME)
        if audit_dir is not None
        else None
    )
    if config is not None:
        backend = DefaultSwsBackend(
            client_factory=AwsClientFactory(config=config),
            execution_mode=execution_mode,
            audit_store=audit_store,
        )
    else:
        backend = DefaultSwsBackend(
            execution_mode=execution_mode, audit_store=audit_store
        )
    SwsMcpServer(backend=backend).run(host=args.host, port=args.port, path=args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())