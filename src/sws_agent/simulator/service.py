"""Conversational simulator service driving the real M4 MCP server.

The service holds one MCP client (the official Streamable HTTP client in
production), keeps a session-scoped activity trace of REAL tool calls, and
turns deterministic commands (from ``router``) into tool invocations whose
structured JSON results become conversational replies and UI blocks.

Every reply is derived from the MCP result actually returned; when a tool
errors or returns a malformed payload the service says so explicitly and
never fabricates a successful outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

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
    "Synthetic demo environment: no AWS access, no real cost figures, "
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
    ) -> None:
        self._client = client
        self.demo = demo
        self.trace: list[dict[str, Any]] = []
        self._last_snapshot: dict[str, Any] | None = None

    async def connect(self) -> None:
        await self._client.connect()

    async def close(self) -> None:
        await self._client.close()

    async def list_available_tools(self) -> list[dict[str, str]]:
        return await self._client.list_tools()

    async def chat(self, message: str, *, history: list[dict] | None = None) -> ChatResponse:
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
        blocks = [
            {
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
        ]
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
        rows = []
        lines = []
        for decision in attention:
            resource_id = decision["resource_id"]
            name = _resource_name(snapshot, resource_id)
            rows.append(
                {
                    "resource_id": resource_id,
                    "name": name,
                    "action": decision.get("recommended_action"),
                    "risk": decision.get("risk_level"),
                    "rule": decision.get("rule"),
                    "rationale": decision.get("rationale"),
                }
            )
            lines.append(f"- {name} -> {decision.get('recommended_action')}")
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
        blocks = [
            {
                "type": "explanation",
                "resource_id": resource_id,
                "name": name,
                "decision": decision,
                "explanation": explanation,
                "interpreted": True,
            }
        ]
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