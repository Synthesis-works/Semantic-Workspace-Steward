"""Conversational simulator service driving the real M4 MCP server.

The service holds one MCP client (the official Streamable HTTP client in
production), keeps a session-scoped activity trace of REAL tool calls, and
turns deterministic commands (from ``router``) into tool invocations whose
structured JSON results become conversational replies and UI blocks.

Every reply is derived from the MCP result actually returned; when a tool
errors or returns a malformed payload the service says so explicitly and
never fabricates a successful outcome.

Two entry paths share this class:

  * Deterministic (no agent configured): ``router`` maps a command to one tool.
  * Conversational (agent configured): a bounded, validated model loop over the
    read-only tool catalog. Explicit approve/deny demo commands are always
    routed deterministically -- the model is never allowed to decide a ticket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .agent import (
    AGENT_HISTORY_LIMIT,
    AGENT_MAX_STEPS,
    AGENT_PROVIDER_LABELS,
    AGENT_TOOL_RESULT_MAX_CHARS,
    AgentActionError,
    AgentError,
    AgentToolRefused,
    COLLECTION_TOOLS,
    ConversationAgent,
    SNAPSHOT_TOOLS,
    build_tool_catalog,
    parse_agent_action,
    render_agent_prompt,
    strip_forbidden,
    validate_agent_tool_call,
)
from .client import McpClient
from .router import Command, route

DEMO_REGION = "us-east-1"
DEMO_DECIDED_BY = "demo-user"

HELP_TEXT = (
    "I'm a rule-based demo assistant driving the real SWS MCP server. "
    "Try:\n"
    "- \"Audit my AWS workspace\"\n"
    "- \"What resources need attention?\"\n"
    '- "Why does <resource> matter?" (e.g. orphan-lambda-no-owner)\n'
    '- "Show my pending approvals"\n'
    '- "Approve this ticket <id>" / "Deny <id>"\n'
    "- \"help\""
)

UNKNOWN_TEXT = (
    "I don't understand that request as a demo command. This is a "
    "rule-based demo router with no autonomous LLM. "
    + HELP_TEXT
)

DEMO_DISCLAIMER = (
    "Synthetic demo environment: no real AWS resources, no real cost figures, "
    "no actions executed."
)


class SimulatorError(Exception):
    """A deterministic failure surfaced to the user as a visible error."""


@dataclass
class ChatResponse:
    reply: str = ""
    blocks: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    demo: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "blocks": self.blocks,
            "trace": self.trace,
            "demo": self.demo,
        }


def _payload_of(result: Any) -> dict[str, Any]:
    """Parse the JSON payload from an MCP CallToolResult content item."""
    content = result.content[0] if result.content else None
    if content is None:
        raise SimulatorError("MCP tool returned no result content")
    structured = getattr(content, "structured_content", None)
    if structured is not None:
        if isinstance(structured, str):
            structured = json.loads(structured)
        return structured
    text = getattr(content, "text", None)
    if text is None:
        raise SimulatorError("MCP tool returned an unreadable result")
    return json.loads(text)


def _attention(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        decision
        for decision in decisions
        if decision.get("recommended_action") != "leave"
    ]


def _resource_name(snapshot: dict[str, Any], resource_id: str) -> str:
    for resource in snapshot.get("resources", []):
        if resource.get("resource_id") == resource_id:
            return resource.get("name") or resource_id
    return resource_id


_SNAPSHOT_ENVELOPE_KEYS = (
    "snapshot_id",
    "created_at",
    "collected_at",
    "run_id",
    "requested_limit",
    "regions",
    "resource_types",
    "counts",
    "partial",
    "truncated",
    "failures",
    "cost",
)


def _minimal_snapshot(
    snapshot: dict[str, Any], resource_id: str
) -> dict[str, Any]:
    """A single-resource snapshot that preserves the deterministic envelope.

    ``explain_resource`` resolves ONE resource; its decision depends only on
    that resource's own fields plus the snapshot completeness flags (no rule
    consumes other resources or relationships), so sending a single-resource
    snapshot keeps the decision -- and its ``snapshot_id``-stamped
    ``decision_id`` -- identical while keeping the MCP payload small. SWS
    builds this from the real snapshot; the model never supplies it.
    """
    envelope: dict[str, Any] = {
        key: snapshot[key] for key in _SNAPSHOT_ENVELOPE_KEYS if key in snapshot
    }
    envelope["resources"] = [
        resource
        for resource in snapshot.get("resources", [])
        if resource.get("resource_id") == resource_id
    ]
    return envelope


def _summary_block(
    snapshot: dict[str, Any], decisions: list[dict[str, Any]]
) -> dict[str, Any]:
    attention = _attention(decisions)
    return {
        "type": "summary",
        "title": "Workspace audit",
        "snapshot_id": snapshot.get("snapshot_id"),
        "resource_count": len(snapshot.get("resources", [])),
        "attention": [
            {
                "resource_id": d["resource_id"],
                "action": d.get("recommended_action"),
                "risk": d.get("risk_level"),
                "rule": d.get("rule"),
                "rationale": d.get("rationale"),
            }
            for d in attention
        ],
    }


def _attention_rows(
    snapshot: dict[str, Any], decisions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for decision in _attention(decisions):
        resource_id = decision["resource_id"]
        rows.append(
            {
                "resource_id": resource_id,
                "name": _resource_name(snapshot, resource_id),
                "action": decision.get("recommended_action"),
                "risk": decision.get("risk_level"),
                "rule": decision.get("rule"),
                "rationale": decision.get("rationale"),
            }
        )
    return rows


def _explanation_block(
    snapshot: dict[str, Any],
    resource_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "explanation",
        "resource_id": resource_id,
        "name": _resource_name(snapshot, resource_id),
        "decision": payload.get("decision", {}),
        "explanation": payload.get("explanation", {}),
        "interpreted": True,
    }


def _observation(payload: dict[str, Any]) -> str:
    """A bounded, forbidden-key-free view of a tool result for the model."""
    projected = strip_forbidden(payload)
    try:
        text = json.dumps(projected, sort_keys=True)
    except (TypeError, ValueError):
        text = json.dumps({"note": "unserializable tool result"})
    if len(text) > AGENT_TOOL_RESULT_MAX_CHARS:
        text = text[:AGENT_TOOL_RESULT_MAX_CHARS] + "…[truncated]"
    return text


class Simulator:
    """Stateless-ish conversational shell over a live MCP client.

    Holds the activity trace for the session and the last audit snapshot so
    that explain/evaluate commands can pass it back through the MCP boundary.
    """

    def __init__(
        self,
        client: McpClient,
        *,
        demo: bool = True,
        agent: ConversationAgent | None = None,
    ) -> None:
        self._client = client
        self.demo = demo
        # ``agent is None`` keeps the fully deterministic, rule-based demo.
        # When set, a bounded model-driven tool loop runs instead, but every
        # tool call still goes through this class's own validated ``_tool``
        # path -- the model never touches MCP (or AWS) directly.
        self._agent = agent
        self.trace: list[dict[str, Any]] = []
        self._last_snapshot: dict[str, Any] | None = None
        self._history: list[dict[str, Any]] = []

    @property
    def mode(self) -> str:
        """``"llm"`` when a conversational agent is configured, else ``"deterministic"``."""
        return "llm" if self._agent is not None else "deterministic"

    @property
    def provider(self) -> str | None:
        """The conversational provider id (e.g. ``"strands"``), or ``None``."""
        agent = self._agent
        if agent is None:
            return None
        return getattr(agent, "provider", None)

    @property
    def model(self) -> str | None:
        """The configured conversational model id, or ``None``."""
        agent = self._agent
        if agent is None:
            return None
        return getattr(agent, "model", None)

    @property
    def provider_label(self) -> str | None:
        """Human-facing provider label for the UI, or ``None``."""
        provider = self.provider
        if provider is None:
            return None
        return AGENT_PROVIDER_LABELS.get(provider, provider)

    async def connect(self) -> None:
        await self._client.connect()

    async def close(self) -> None:
        await self._client.close()

    async def list_available_tools(self) -> list[dict[str, str]]:
        return await self._client.list_tools()

    async def chat(self, message: str, *, history: list[dict] | None = None) -> ChatResponse:
        if self._agent is not None:
            # Explicit approve/deny demo commands stay on the deterministic
            # path: the conversational agent is never allowed to decide a
            # ticket (``decide_ticket`` is absent from its catalog). Every
            # other message goes to the bounded conversational loop.
            explicit = route(message)
            if explicit.intent == "decide":
                try:
                    return self._finalize(await self._execute(explicit))
                except SimulatorError as exc:
                    return self._finalize(self._error_response(str(exc)))
                except Exception as exc:  # pragmatic demo boundary: any failure is visible
                    return self._finalize(
                        self._error_response(f"simulator error: {exc!r}")
                    )
            try:
                return self._finalize(await self._chat_with_agent(message))
            except SimulatorError as exc:
                return self._finalize(self._error_response(str(exc)))
            except Exception as exc:  # pragmatic demo boundary: any failure is visible
                return self._finalize(
                    self._error_response(f"simulator error: {exc!r}")
                )
        command = route(message)
        try:
            if command.intent == "unknown":
                return self._finalize(self._no_tool_reply(UNKNOWN_TEXT))
            if command.intent == "help":
                return self._finalize(self._no_tool_reply(HELP_TEXT))
            return self._finalize(await self._execute(command))
        except SimulatorError as exc:
            return self._finalize(self._error_response(str(exc)))
        except Exception as exc:  # pragmatic demo boundary: any failure is visible
            return self._finalize(self._error_response(f"simulator error: {exc!r}"))

    def _finalize(self, response: ChatResponse) -> ChatResponse:
        if self.demo and DEMO_DISCLAIMER not in response.reply:
            response.reply = f"{response.reply}\n\n{DEMO_DISCLAIMER}"
        response.trace = list(self.trace)
        return response

    def _no_tool_reply(self, text: str) -> ChatResponse:
        self.trace.append(
            {
                "tool": "router",
                "state": "success",
                "summary": "no MCP tool needed",
                "payload": None,
            }
        )
        return ChatResponse(reply=text)

    def _error_response(self, message: str) -> ChatResponse:
        self.trace.append(
            {
                "tool": "chat",
                "state": "error",
                "summary": message,
                "payload": None,
            }
        )
        reply = (
            "SWS could not complete that request. "
            f"Error: {message}"
        )
        return ChatResponse(
            reply=reply,
            blocks=[{"type": "error", "message": message}],
            demo=self.demo,
        )

    async def _tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.trace.append(
            {
                "tool": name,
                "state": "running",
                "summary": f"calling {name}",
                "payload": None,
            }
        )
        try:
            result = await self._client.call_tool(name, arguments)
        except Exception as exc:
            self._mark_trace(name, "error", f"{name} failed: {exc!r}")
            raise SimulatorError(f"tool {name} could not be called: {exc!r}") from exc
        if getattr(result, "is_error", False):
            text = (result.content[0].text if result.content else None) or ""
            self._mark_trace(name, "error", f"{name} returned an MCP error")
            raise SimulatorError(f"tool {name} returned an error: {text}")
        try:
            payload = _payload_of(result)
        except (json.JSONDecodeError, SimulatorError) as exc:
            self._mark_trace(name, "error", f"{name} returned malformed data")
            raise SimulatorError(
                f"tool {name} returned malformed structured data"
            ) from exc
        self._mark_trace(name, "success", self._summarize(name, payload))
        return payload

    def _mark_trace(
        self, name: str, state: str, summary: str
    ) -> None:
        for event in reversed(self.trace):
            if event["tool"] == name and event["state"] == "running":
                event["state"] = state
                event["summary"] = summary
                return
        self.trace.append(
            {"tool": name, "state": state, "summary": summary, "payload": None}
        )

    @staticmethod
    def _summarize(name: str, payload: dict[str, Any]) -> str:
        if name == "audit_workspace":
            snapshot = payload.get("snapshot", {})
            attention_count = len(
                _attention(payload.get("decisions", []))
            )
            return (
                f"collected {len(snapshot.get('resources', []))} resources; "
                f"{attention_count} need attention"
            )
        if name == "evaluate_workspace":
            attention_count = len(_attention(payload.get("decisions", [])))
            return f"{attention_count} resources need attention"
        if name == "explain_resource":
            return "explanation returned for one resource"
        if name == "list_approvals":
            return f"{len(payload.get('approvals', []))} pending approval tickets"
        if name == "decide_ticket":
            ticket = payload.get("ticket", {})
            return f"ticket {ticket.get('ticket_id')} -> {ticket.get('status')}"
        return f"{name} completed"

    async def _ensure_audit(
        self,
    ) -> dict[str, Any]:
        if self._last_snapshot is not None:
            return self._last_snapshot
        payload = await self._tool(
            "audit_workspace", {"regions": [DEMO_REGION]}
        )
        self._last_snapshot = payload["snapshot"]
        return self._last_snapshot

    async def _execute(self, command: Command) -> ChatResponse:
        if command.intent == "audit":
            return await self._run_audit()
        if command.intent == "attention":
            return await self._run_attention()
        if command.intent == "explain":
            return await self._run_explain(command)
        if command.intent == "approvals":
            return await self._run_approvals()
        if command.intent == "decide":
            return await self._run_decide(command)
        return self._no_tool_reply(UNKNOWN_TEXT)

    async def _run_audit(self) -> ChatResponse:
        payload = await self._tool(
            "audit_workspace", {"regions": [DEMO_REGION]}
        )
        self._last_snapshot = payload["snapshot"]
        snapshot = payload["snapshot"]
        counts = snapshot.get("counts", {})
        count_text = ", ".join(
            f"{key}={value}" for key, value in sorted(counts.items())
        )
        attention = _attention(payload.get("decisions", []))
        names = ", ".join(
            _resource_name(snapshot, d["resource_id"]) for d in attention
        ) or "none"
        reply = (
            f"Audit complete. Collected {len(snapshot.get('resources', []))} "
            f"resources across {len(snapshot.get('regions', []))} regions "
            f"({count_text}). {len(attention)} resource(s) need attention: "
            f"{names}."
        )
        blocks = [_summary_block(snapshot, payload.get("decisions", []))]
        return ChatResponse(reply=reply, blocks=blocks, demo=self.demo)

    async def _run_attention(self) -> ChatResponse:
        snapshot = await self._ensure_audit()
        payload = await self._tool(
            "evaluate_workspace", {"snapshot": snapshot}
        )
        attention = _attention(payload.get("decisions", []))
        if not attention:
            return ChatResponse(
                reply="No resources currently require attention.",
                blocks=[{"type": "attention", "resources": []}],
                demo=self.demo,
            )
        rows = _attention_rows(snapshot, payload.get("decisions", []))
        lines = [f"- {r['name']} -> {r['action']}" for r in rows]
        reply = (
            f"{len(attention)} resource(s) need attention:\n" + "\n".join(lines)
        )
        return ChatResponse(
            reply=reply,
            blocks=[{"type": "attention", "resources": rows}],
            demo=self.demo,
        )

    async def _run_explain(self, command: Command) -> ChatResponse:
        snapshot = await self._ensure_audit()
        resource_id = command.resource_id
        if resource_id is None:
            attention_payload = await self._tool(
                "evaluate_workspace", {"snapshot": snapshot}
            )
            attention = _attention(attention_payload.get("decisions", []))
            if not attention:
                raise SimulatorError(
                    "no resource in the demo workspace needs attention, so "
                    "there is nothing to explain yet"
                )
            resource_id = attention[0]["resource_id"]
        payload = await self._tool(
            "explain_resource",
            {"snapshot": snapshot, "resource_id": resource_id},
        )
        explanation = payload["explanation"]
        decision = payload["decision"]
        name = _resource_name(snapshot, resource_id)
        reply = (
            f"Deterministic policy decision for {name}: "
            f"{decision.get('recommended_action')}"
            f"{' (rule ' + decision.get('rule') + ')' if decision.get('rule') else ''}. "
            "Meaning (interpretive demo explanation): "
            f"{explanation.get('text') or 'none'}"
        )
        blocks = [_explanation_block(snapshot, resource_id, payload)]
        return ChatResponse(reply=reply, blocks=blocks, demo=self.demo)

    async def _run_approvals(self) -> ChatResponse:
        payload = await self._tool("list_approvals", {})
        tickets = payload.get("approvals", [])
        if not tickets:
            return ChatResponse(
                reply="No pending approval tickets.",
                blocks=[{"type": "tickets", "tickets": []}],
                demo=self.demo,
            )
        lines = [
            f"- {t['ticket_id']}: {t['resource_id']} "
            f"({t['action']}) — {t['rationale']}"
            for t in tickets
        ]
        reply = (
            f"{len(tickets)} pending approval ticket(s):\n"
            + "\n".join(lines)
            + "\n\nUse \"Approve this ticket <id>\" or \"Deny <id>\". "
            "This only updates ticket state; nothing executes."
        )
        return ChatResponse(
            reply=reply,
            blocks=[{"type": "tickets", "tickets": tickets}],
            demo=self.demo,
        )

    async def _run_decide(self, command: Command) -> ChatResponse:
        ticket_id = command.ticket_id
        if ticket_id is None:
            tickets_payload = await self._tool("list_approvals", {})
            tickets = tickets_payload.get("approvals", [])
            if not tickets:
                raise SimulatorError(
                    "there are no pending approval tickets to decide"
                )
            ticket_id = tickets[0]["ticket_id"]
        decision = command.decision or "grant"
        payload = await self._tool(
            "decide_ticket",
            {
                "ticket_id": ticket_id,
                "decision": decision,
                "decided_by": DEMO_DECIDED_BY,
                "reason": "demo decision",
            },
        )
        ticket = payload["ticket"]
        reply = (
            f"Ticket {ticket.get('ticket_id')} "
            f"{ticket.get('status')}. No AWS action was executed; the approval "
            "store (in-memory, demo) was updated only."
        )
        return ChatResponse(
            reply=reply,
            blocks=[
                {"type": "ticket_update", "ticket": ticket, "decision": decision}
            ],
            demo=self.demo,
        )

    async def _resolve_agent_arguments(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = dict(arguments)
        # Never trust a model-supplied snapshot; SWS injects the real one.
        resolved.pop("snapshot", None)
        if name in SNAPSHOT_TOOLS:
            snapshot = await self._ensure_audit()
            resource_id = resolved.get("resource_id")
            if name == "explain_resource" and isinstance(resource_id, str):
                snapshot = _minimal_snapshot(snapshot, resource_id)
            resolved["snapshot"] = snapshot
        if name in COLLECTION_TOOLS:
            resolved.setdefault("regions", [DEMO_REGION])
            # The conversational demo audit is deterministic and never collects
            # cost; a model-invented, non-ISO cost_end_date can cause tool
            # failures, so strip cost arguments entirely.
            for key in (
                "collect_cost",
                "cost_end_date",
                "cost_window_days",
                "cost_group_by",
            ):
                resolved.pop(key, None)
        return resolved

    def _blocks_for(
        self, collected: list[tuple[str, dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for name, payload in collected:
            if name in ("audit_workspace", "collect_workspace"):
                blocks.append(
                    _summary_block(
                        payload.get("snapshot", {}), payload.get("decisions", [])
                    )
                )
            elif name == "evaluate_workspace":
                blocks.append(
                    {
                        "type": "attention",
                        "resources": _attention_rows(
                            self._last_snapshot or {}, payload.get("decisions", [])
                        ),
                    }
                )
            elif name == "explain_resource":
                decision = payload.get("decision", {})
                resource_id = decision.get("resource_id") or ""
                blocks.append(
                    _explanation_block(
                        self._last_snapshot or {}, resource_id, payload
                    )
                )
            elif name == "list_approvals":
                blocks.append(
                    {"type": "tickets", "tickets": payload.get("approvals", [])}
                )
        return blocks

    @staticmethod
    def _agent_summary_reply(collected: list[tuple[str, dict[str, Any]]]) -> str:
        for name, payload in collected:
            if name in ("audit_workspace", "collect_workspace"):
                snapshot = payload.get("snapshot", {})
                attention = len(_attention(payload.get("decisions", [])))
                return (
                    f"Audit complete. Collected "
                    f"{len(snapshot.get('resources', []))} resources; "
                    f"{attention} need attention."
                )
        return "I gathered the information you asked for from the SWS tools."

    def _remember(self, reply: str) -> None:
        self._history.append({"role": "assistant", "content": reply})
        if len(self._history) > AGENT_HISTORY_LIMIT:
            self._history = self._history[-AGENT_HISTORY_LIMIT:]

    def _agent_degraded_response(self, message: str) -> ChatResponse:
        self.trace.append(
            {
                "tool": "agent",
                "state": "error",
                "summary": f"conversational model unavailable: {message}",
                "payload": None,
            }
        )
        reply = (
            "The conversational model is unavailable, so I did not answer "
            "this turn. You can still use the deterministic demo commands, "
            'for example "Audit my AWS workspace".'
        )
        return ChatResponse(
            reply=reply,
            blocks=[{"type": "error", "message": f"model unavailable: {message}"}],
            demo=self.demo,
        )

    def _refusal_response(self, message: str, tool_name: str | None) -> ChatResponse:
        tool = tool_name or "agent"
        self.trace.append(
            {
                "tool": tool,
                "state": "error",
                "summary": f"refused: {message}",
                "payload": None,
            }
        )
        reply = (
            "I refused that request for safety: it was not a valid SWS demo "
            f"tool call ({message}). No MCP tool was invoked."
        )
        return ChatResponse(
            reply=reply,
            blocks=[{"type": "error", "message": message}],
            demo=self.demo,
        )

    async def _chat_with_agent(self, message: str) -> ChatResponse:
        self._history.append({"role": "user", "content": message})
        collected: list[tuple[str, dict[str, Any]]] = []
        for _ in range(AGENT_MAX_STEPS):
            catalog = build_tool_catalog()
            prompt = render_agent_prompt(transcript=self._history, catalog=catalog)
            try:
                raw = self._agent.respond(prompt)  # type: ignore[operator]
            except AgentError as exc:
                return self._agent_degraded_response(str(exc))
            except Exception as exc:  # pragma: no cover
                return self._agent_degraded_response(f"model error: {exc!r}")
            try:
                action = parse_agent_action(raw)
            except AgentActionError as exc:
                return self._agent_degraded_response(str(exc))
            if action.kind == "answer":
                reply = (action.text or "").strip() or self._agent_summary_reply(
                    collected
                )
                self._remember(reply)
                return ChatResponse(
                    reply=reply, blocks=self._blocks_for(collected), demo=self.demo
                )
            try:
                validate_agent_tool_call(action.tool_name, action.arguments)
            except AgentToolRefused as exc:
                return self._refusal_response(str(exc), action.tool_name)
            try:
                arguments = await self._resolve_agent_arguments(
                    action.tool_name or "", action.arguments or {}
                )
                payload = await self._tool(action.tool_name or "", arguments)
            except SimulatorError as exc:
                return self._error_response(str(exc))
            collected.append((action.tool_name or "", payload))
            self._history.append(
                {
                    "role": "tool",
                    "name": action.tool_name or "",
                    "content": _observation(payload),
                }
            )
        reply = self._agent_summary_reply(collected)
        self._remember(reply)
        return ChatResponse(
            reply=reply, blocks=self._blocks_for(collected), demo=self.demo
        )