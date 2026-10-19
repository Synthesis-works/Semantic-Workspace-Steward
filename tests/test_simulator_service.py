"""Hermetic service-level tests for the M5 simulator.

The simulator is driven with a scripted fake MCP client that mirrors the
shapes the official MCP SDK returns, so these tests never need a server or
AWS. The live-wire variant is covered in test_simulator_mcp.py.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from sws_agent.simulator.service import DEMO_DISCLAIMER, Simulator

TOOL_NAMES = [
    "audit_workspace",
    "collect_workspace",
    "decide_ticket",
    "evaluate_workspace",
    "explain_resource",
    "get_cost_estimates",
    "get_relationships",
    "list_approvals",
]

SNAPSHOT = {
    "snapshot_id": "demo-synthetic-workspace",
    "regions": ["us-east-1"],
    "counts": {"s3_bucket": 12, "lambda_function": 8},
    "resources": [
        {
            "resource_id": "web-assets-cdn",
            "name": "web-assets-cdn",
            "resource_type": "s3_bucket",
            "region": "us-east-1",
            "owner_tag": "web",
        },
        {
            "resource_id": "ghost-bucket-no-owner",
            "name": "ghost-bucket-no-owner",
            "resource_type": "s3_bucket",
            "region": "us-east-1",
            "owner_tag": None,
        },
    ],
}

DECISIONS = [
    {
        "resource_id": "web-assets-cdn",
        "recommended_action": "leave",
        "risk_level": "none",
        "rationale": "owned and healthy",
        "rule": None,
        "confidence": 1.0,
    },
    {
        "resource_id": "ghost-bucket-no-owner",
        "recommended_action": "flag_for_review",
        "risk_level": "medium",
        "rationale": "no owner tag",
        "rule": "missing_owner_tag",
        "confidence": 1.0,
    },
]

TICKETS = [
    {
        "ticket_id": "demo-ticket-0001",
        "resource_id": "ghost-bucket-no-owner",
        "action": "stop_resource",
        "rationale": "unattributed bucket",
        "status": "pending",
        "created_at": "2026-01-02T00:00:00Z",
        "decided_at": None,
        "decided_by": "",
        "decision_reason": "",
    }
]


def _payload(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "audit_workspace": {
            "snapshot": SNAPSHOT,
            "decisions": DECISIONS,
            "relationships": [],
        },
        "evaluate_workspace": {"decisions": DECISIONS},
        "explain_resource": {
            "explanation": {
                "text": "high-cost unattributed storage",
                "claim_kind": "interpreted",
                "provider": "demo",
                "reason": "synthetic demo",
            },
            "decision": DECISIONS[1],
        },
        "list_approvals": {"approvals": TICKETS},
        "decide_ticket": {
            "ticket": {
                **TICKETS[0],
                "status": "granted",
                "decided_at": "2026-01-02T00:00:01Z",
                "decided_by": "demo-user",
                "decision_reason": "demo decision",
            }
        },
        "get_relationships": {"relationships": []},
        "collect_workspace": {"snapshot": SNAPSHOT},
        "get_cost_estimates": {"cost_estimates": []},
    } | settings


class ScriptedClient:
    """Fake McpClient mirroring the official SDK return shapes."""

    def __init__(
        self,
        *,
        responses: dict[str, Any] | None = None,
        exceptions: frozenset[str] = frozenset(),
        tools: list[dict[str, str]] | None = None,
    ) -> None:
        self._responses = _payload(responses or {})
        self._exceptions = exceptions
        self._tools = tools or [
            {"name": name, "description": ""} for name in TOOL_NAMES
        ]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:  # pragma: no cover - trivial
        return None

    async def close(self) -> None:  # pragma: no cover - trivial
        return None

    async def list_tools(self) -> list[dict[str, str]]:
        return list(self._tools)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Any:
        self.calls.append((name, dict(arguments)))
        if name in self._exceptions:
            raise RuntimeError("connection boom")
        spec = self._responses[name]
        if isinstance(spec, tuple):
            kind, value = spec
            if kind == "is_error":
                return SimpleNamespace(
                    is_error=True,
                    content=[SimpleNamespace(text=value, structured_content=None)],
                )
            if kind == "malformed":
                return SimpleNamespace(
                    is_error=False,
                    content=[
                        SimpleNamespace(
                            text="this is not json {", structured_content=None
                        )
                    ],
                )
            raise AssertionError(f"unknown scripted response kind {kind!r}")
        return SimpleNamespace(
            is_error=False,
            content=[
                SimpleNamespace(text=json.dumps(spec), structured_content=None)
            ],
        )


def _run(coro) -> Any:
    return asyncio.run(coro)


def _simulator(client: ScriptedClient) -> Simulator:
    return Simulator(client)


def test_simulator_lists_exactly_the_real_m4_tools() -> None:
    client = ScriptedClient()
    tools = _run(_simulator(client).list_available_tools())
    names = {tool["name"] for tool in tools}
    assert names == set(TOOL_NAMES)
    assert not any("execute" in name for name in names)
    assert not any("apply" in name for name in names)


def test_audit_flow_invokes_audit_workspace_and_renders_result() -> None:
    client = ScriptedClient()
    response = _run(_simulator(client).chat("Audit my AWS workspace"))
    assert client.calls == [("audit_workspace", {"regions": ["us-east-1"]})]
    assert "Audit complete" in response.reply
    assert "12" in response.reply
    assert response.blocks[0]["type"] == "summary"
    assert response.blocks[0]["snapshot_id"] == "demo-synthetic-workspace"
    attention = response.blocks[0]["attention"]
    assert {row["resource_id"] for row in attention} == {"ghost-bucket-no-owner"}


def test_trace_reflects_actual_tool_calls() -> None:
    client = ScriptedClient()
    response = _run(_simulator(client).chat("Audit my AWS workspace"))
    tools = [event["tool"] for event in response.trace]
    assert tools == ["audit_workspace"]
    assert all(
        event["state"] == "success" for event in response.trace
    )


def test_attention_triggers_auto_audit_then_evaluate() -> None:
    client = ScriptedClient()
    response = _run(
        _simulator(client).chat("What resources need attention?")
    )
    assert [name for name, _ in client.calls] == [
        "audit_workspace",
        "evaluate_workspace",
    ]
    assert response.blocks[0]["type"] == "attention"
    assert {
        row["resource_id"] for row in response.blocks[0]["resources"]
    } == {"ghost-bucket-no-owner"}
    assert [event["tool"] for event in response.trace] == [
        "audit_workspace",
        "evaluate_workspace",
    ]


def test_explanation_flow_calls_explain_resource() -> None:
    client = ScriptedClient()
    sim = _simulator(client)
    _run(sim.chat("Audit my AWS workspace"))
    response = _run(sim.chat("Why does ghost-bucket-no-owner matter?"))
    exec_calls = [name for name, _ in client.calls]
    assert "explain_resource" in exec_calls
    explain_args = dict(client.calls[-1][1])
    assert explain_args["resource_id"] == "ghost-bucket-no-owner"
    assert explain_args["snapshot"]["snapshot_id"] == "demo-synthetic-workspace"
    assert "Deterministic policy decision" in response.reply
    assert response.blocks[0]["type"] == "explanation"
    assert response.blocks[0]["interpreted"] is True


EC2_SNAPSHOT = {
    "snapshot_id": "demo-synthetic-workspace",
    "regions": ["us-east-1"],
    "counts": {"s3_bucket": 12, "lambda_function": 8, "ec2_instance": 3},
    "resources": [
        {
            "resource_id": "web-assets-api",
            "name": "web-assets-api",
            "resource_type": "ec2_instance",
            "region": "us-east-1",
            "owner_tag": "web",
            "state": "running",
        },
        {
            "resource_id": "legacy-reporting-host",
            "name": "legacy-reporting-host",
            "resource_type": "ec2_instance",
            "region": "us-east-1",
            "owner_tag": "ops",
            "state": "stopped",
        },
    ],
}

EC2_DECISIONS = [
    {
        "resource_id": "web-assets-api",
        "recommended_action": "stop_resource",
        "risk_level": "medium",
        "rationale": "running instance; a stop is valid and reversible",
        "rule": "ec2_stop_eligible",
        "confidence": 1.0,
    },
    {
        "resource_id": "legacy-reporting-host",
        "recommended_action": "leave",
        "risk_level": "none",
        "rationale": "already stopped",
        "rule": "ec2_stop_not_eligible",
        "confidence": 1.0,
    },
]

PLAN = {
    "plan": {
        "action_plan_id": "plan-0001",
        "created_at": "2026-01-02T00:00:00Z",
        "resource_id": "web-assets-api",
        "action": "stop_resource",
        "execution_mode": "safe",
        "authorization": {
            "decision": "pending_approval",
            "requires_human_approval": True,
            "reason": "STOP_RESOURCE requires human approval in safe mode",
        },
        "ticket": {
            "ticket_id": "demo-ticket-0001",
            "resource_id": "web-assets-api",
            "action": "stop_resource",
            "status": "pending",
        },
        "executed": False,
    }
}


def test_briefing_renders_card_and_opens_approval_ticket() -> None:
    client = ScriptedClient(
        responses={
            "audit_workspace": (
                {"snapshot": EC2_SNAPSHOT, "decisions": EC2_DECISIONS}
            ),
            "explain_resource": {
                "explanation": {"text": "running instance eligible for a stop"},
                "decision": EC2_DECISIONS[0],
            },
            "request_approval": PLAN,
        }
    )
    response = _run(
        _simulator(client).chat(
            "What would happen if I stopped web-assets-api?"
        )
    )
    assert [name for name, _ in client.calls] == [
        "audit_workspace",
        "explain_resource",
        "request_approval",
    ]
    assert "Cloud Action Briefing" in response.reply
    block = response.blocks[0]
    assert block["type"] == "briefing"
    assert block["name"] == "web-assets-api"
    assert block["resource_type"] == "ec2_instance"
    assert block["state"] == "running"
    assert block["policy"]["action"] == "stop_resource"
    assert block["proposed_action"] == "stop_resource"
    assert block["authorization"]["requires_human_approval"] is True
    assert block["ticket"]["ticket_id"] == "demo-ticket-0001"
    assert block["execution"] == "not_available_in_public_demo"


def test_briefing_without_resource_picks_first_running_instance() -> None:
    client = ScriptedClient(
        responses={
            "audit_workspace": (
                {"snapshot": EC2_SNAPSHOT, "decisions": EC2_DECISIONS}
            ),
            "explain_resource": {
                "explanation": {"text": "running instance eligible for a stop"},
                "decision": EC2_DECISIONS[0],
            },
            "request_approval": PLAN,
        }
    )
    _run(_simulator(client).chat("Give me a cloud action briefing"))
    assert dict(client.calls[-2][1])["resource_id"] == "web-assets-api"


def test_briefing_on_non_stop_resource_explains_without_plan() -> None:
    client = ScriptedClient()
    response = _run(
        _simulator(client).chat(
            "What would happen if I stopped ghost-bucket-no-owner?"
        )
    )
    assert [name for name, _ in client.calls] == [
        "audit_workspace",
        "explain_resource",
    ]
    assert response.blocks[0]["type"] == "explanation"
    assert "not eligible for a stop" in response.reply


def test_approvals_flow_calls_list_approvals() -> None:
    client = ScriptedClient()
    response = _run(_simulator(client).chat("Show my pending approvals"))
    assert client.calls == [("list_approvals", {})]
    assert "demo-ticket-0001" in response.reply
    assert response.blocks[0]["type"] == "tickets"


def test_decide_grant_calls_decide_ticket() -> None:
    client = ScriptedClient()
    response = _run(
        _simulator(client).chat("Approve this ticket demo-ticket-0001")
    )
    assert [name for name, _ in client.calls] == ["decide_ticket"]
    args = dict(client.calls[0][1])
    assert args["ticket_id"] == "demo-ticket-0001"
    assert args["decision"] == "grant"
    assert "no aws action was executed" in response.reply.lower()
    assert response.blocks[0]["type"] == "ticket_update"


def test_decide_without_id_lists_then_decides_first_ticket() -> None:
    client = ScriptedClient()
    response = _run(_simulator(client).chat("deny all"))
    assert [name for name, _ in client.calls] == [
        "list_approvals",
        "decide_ticket",
    ]
    assert dict(client.calls[1][1])["ticket_id"] == "demo-ticket-0001"
    assert dict(client.calls[1][1])["decision"] == "deny"
    assert "no aws action was executed" in response.reply.lower()


def test_unknown_command_is_deterministic_and_uses_no_tool() -> None:
    client = ScriptedClient()
    first = _run(_simulator(client).chat("do a barrel roll"))
    second = _run(_simulator(client).chat("do a barrel roll"))
    assert first.reply == second.reply
    assert "I don't understand" in first.reply
    assert client.calls == []
    assert first.trace and first.trace[-1]["state"] == "success"


def test_help_uses_no_tool_and_lists_commands() -> None:
    client = ScriptedClient()
    response = _run(_simulator(client).chat("help"))
    assert client.calls == []
    assert "demo assistant" in response.reply


def test_malformed_mcp_response_is_visible_error_not_fabricated() -> None:
    client = ScriptedClient(responses={"audit_workspace": ("malformed", None)})
    response = _run(_simulator(client).chat("Audit my AWS workspace"))
    assert "could not complete" in response.reply.lower()
    assert "malformed" in response.reply.lower()
    assert response.blocks and response.blocks[0]["type"] == "error"
    assert response.trace[-1]["state"] == "error"


def test_mcp_is_error_is_visible_error_with_no_success_claim() -> None:
    client = ScriptedClient(
        responses={"list_approvals": ("is_error", "approval store unavailable")}
    )
    response = _run(_simulator(client).chat("Show my pending approvals"))
    assert "could not complete" in response.reply.lower()
    assert "approval store unavailable" in response.reply
    assert response.blocks and response.blocks[0]["type"] == "error"


def test_connection_failure_is_visible_error() -> None:
    client = ScriptedClient(
        exceptions=frozenset({"audit_workspace"}),
    )
    response = _run(_simulator(client).chat("Audit my AWS workspace"))
    assert "could not complete" in response.reply.lower()
    assert response.blocks and response.blocks[0]["type"] == "error"


@pytest.mark.parametrize(
    "message",
    [
        "Audit my AWS workspace",
        "What resources need attention?",
        "Why does ghost-bucket-no-owner matter?",
        "Show my pending approvals",
        "Approve this ticket demo-ticket-0001",
        "help",
        "do a barrel roll",
    ],
)
def test_every_reply_carries_the_demo_disclaimer(message: str) -> None:
    client = ScriptedClient()
    response = _run(_simulator(client).chat(message))
    assert response.demo is True
    assert DEMO_DISCLAIMER in response.reply