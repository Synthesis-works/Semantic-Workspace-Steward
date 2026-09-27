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
  - Handlers are stateless. No persistent sessions, no database, no external
    state. Stateful workspace data travels with the client: tools that need a
    snapshot accept the structured snapshot returned by ``collect_workspace``
    or ``audit_workspace``.
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

from mcp.server.mcpserver import MCPServer as SdkMCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from ..approval import ApprovalError, InMemoryApprovalStore, UnknownTicketError
from ..aws import AwsClientFactory, aws_config_from_env
from ..cost_explorer import CostExplorerCollector
from ..explanation import NullExplanationProvider
from ..models import (
    ApprovalTicket,
    CostEstimate,
    ExplanationResult,
    PolicyDecision,
    ResourceRecord,
    WorkspaceSnapshot,
)
from ..policy import evaluate_workspace
from ..relationships import WorkspaceSnapshotTooLargeError, derive_relationships
from ..workspace import collect_workspace as _collect_workspace
from . import ToolHandler, ToolRegistry

SERVER_NAME: str = "sws"
SERVER_VERSION: str = "0.1.0"
SERVER_DESCRIPTION: str = (
    "SWS (Semantic Workspace Steward): deterministic AWS workspace inventory, "
    "relationships, policy, cost, and explanation over MCP."
)

# Tools are intentionally limited to read-only analysis and the approval
# ticket lifecycle. No action-execution (e.g. STOP_RESOURCE) tool is exposed.
BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "audit_workspace",
    "collect_workspace",
    "get_relationships",
    "evaluate_workspace",
    "get_cost_estimates",
    "explain_resource",
    "list_approvals",
    "decide_ticket",
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
    ) -> list[CostEstimate]: ...

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
    ) -> None:
        self._client_factory = client_factory
        self._explainer = explainer if explainer is not None else NullExplanationProvider()
        self._approval_store = (
            approval_store if approval_store is not None else InMemoryApprovalStore()
        )

    def _client(self) -> Any:
        if self._client_factory is None:
            raise ToolError(
                "no AWS client factory configured for this server"
            )
        return self._client_factory()

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
        return _collect_workspace(
            client=self._client(),
            regions=regions,
            limit=limit,
            collect_cost=collect_cost,
            cost_window_days=cost_window_days,
            cost_group_by=cost_group_by,
            cost_end_date=cost_end_date,
        )

    def derive_relationships(
        self, snapshot: WorkspaceSnapshot
    ) -> list[Any]:
        return derive_relationships(snapshot)

    def evaluate_workspace(
        self,
        snapshot: WorkspaceSnapshot,
        relationships: list[Any] | None = None,
    ) -> list[PolicyDecision]:
        return evaluate_workspace(snapshot, relationships=relationships)

    def get_cost_estimates(
        self,
        *,
        end_date: date,
        window_days: int | None = None,
        group_by: list[str] | None = None,
    ) -> list[CostEstimate]:
        return CostExplorerCollector(self._client()).collect(
            window_days=window_days,
            end_date=end_date,
            group_by=group_by,
        )

    def explain(
        self, resource: ResourceRecord, decision: PolicyDecision
    ) -> ExplanationResult:
        return self._explainer.explain(resource, decision)

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
            return self._approval_store.grant(
                ticket_id, decided_by=decided_by, reason=reason
            )
        if decision == "deny":
            return self._approval_store.deny(
                ticket_id, decided_by=decided_by, reason=reason
            )
        raise ToolError("decision must be 'grant' or 'deny'")


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


def _adapter_functions(
    backend: SwsBackend,
) -> list[tuple[str, str, Callable[..., dict[str, Any]]]]:
    """The eight built-in tools: (name, description, typed handler)."""

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
        estimates = backend.get_cost_estimates(
            end_date=parsed_end_date,
            window_days=window_days,
            group_by=group_by,
        )
        return {
            "cost_estimates": [
                _jsonable(e.model_dump()) for e in estimates
            ]
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
    """
    parser = argparse.ArgumentParser(prog="sws-mcp-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/mcp")
    args = parser.parse_args(argv)
    backend = None
    config = aws_config_from_env()
    if config is not None:
        backend = DefaultSwsBackend(client_factory=AwsClientFactory(config=config))
    SwsMcpServer(backend=backend).run(host=args.host, port=args.port, path=args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())