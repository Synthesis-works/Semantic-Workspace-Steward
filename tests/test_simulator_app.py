"""Hermetic tests for the starlette web layer of the M5 simulator.

Uses an in-process fake MCP client and httpx ASGITransport: no sockets are
bound, no network is touched, and no MCP server is started. This is where
the "simulator builds and serves its endpoints" behavior lives.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from sws_agent.simulator.app import create_app
from sws_agent.simulator.service import Simulator

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


class FakeClient:
    """Minimal in-process fake McpClient (mirrors SDK result shapes)."""

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
            "rationale": "owned",
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

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = {
            "audit_workspace": {
                "snapshot": self.SNAPSHOT,
                "decisions": self.DECISIONS,
                "relationships": [],
            },
            "evaluate_workspace": {"decisions": self.DECISIONS},
            "list_approvals": {"approvals": []},
            "decide_ticket": {"ticket": {"ticket_id": "-", "status": "granted"}},
            "explain_resource": {
                "explanation": {
                    "text": "demo",
                    "claim_kind": "interpreted",
                    "provider": "demo",
                    "reason": "synthetic",
                },
                "decision": self.DECISIONS[1],
            },
        }

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def list_tools(self) -> list[dict[str, str]]:
        return [{"name": name, "description": ""} for name in TOOL_NAMES]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        payload = self._responses.get(name, {"ok": True, "name": name})
        return SimpleNamespace(
            is_error=False,
            content=[
                SimpleNamespace(
                    text=json.dumps(payload),
                    structured_content=None,
                )
            ],
        )


def _run(coro) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def app():
    client = FakeClient()
    simulator = Simulator(client)
    return create_app(simulator=simulator), client


def test_app_builds_without_binding_a_socket(app) -> None:
    starlette_app, _ = app
    names = {route.path for route in starlette_app.routes}
    assert "/" in names
    assert "/api/chat" in names
    assert "/api/commands" in names
    assert "/api/meta" in names
    assert "/api/tools" in names
    assert starlette_app.state.simulator is not None


def test_index_serves_the_web_ui(app) -> None:
    starlette_app, _ = app
    async def _exercise() -> httpx.Response:
        async with _client_for(starlette_app) as client:
            return await client.get("/")

    response = _run(_exercise())
    assert response.status_code == 200
    assert "SWS interactive demo" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_chat_endpoint_returns_structured_conversation(app) -> None:
    starlette_app, fake = app
    async def _exercise() -> httpx.Response:
        async with _client_for(starlette_app) as client:
            return await client.post(
                "/api/chat",
                json={"message": "help", "history": []},
            )

    response = _run(_exercise())
    assert response.status_code == 200
    body = response.json()
    assert body["demo"] is True
    assert "rule-based demo assistant" in body["reply"]
    assert body["blocks"] == []
    assert fake.calls == []
    assert body["trace"]


def test_chat_endpoint_audit_invokes_tool_through_mcp(app) -> None:
    starlette_app, fake = app
    async def _exercise() -> httpx.Response:
        async with _client_for(starlette_app) as client:
            return await client.post(
                "/api/chat",
                json={"message": "Audit my AWS workspace", "history": []},
            )

    response = _run(_exercise())
    assert response.status_code == 200
    body = response.json()
    assert fake.calls == [("audit_workspace", {"regions": ["us-east-1"]})]
    assert "Audit complete" in body["reply"]
    assert body["trace"][0]["tool"] == "audit_workspace"
    assert body["trace"][0]["state"] == "success"
    assert body["blocks"][0]["attention"]


def test_commands_and_meta_endpoints(app) -> None:
    starlette_app, _ = app
    async def _exercise() -> tuple[httpx.Response, httpx.Response]:
        async with _client_for(starlette_app) as client:
            commands = await client.get("/api/commands")
            meta = await client.get("/api/meta")
            return commands, meta

    commands, meta = _run(_exercise())
    assert commands.status_code == 200
    assert "Audit my AWS workspace" in commands.json()["commands"]
    assert meta.status_code == 200
    assert meta.json()["demo"] is True


def test_tools_endpoint_lists_no_action_execution_tool(app) -> None:
    starlette_app, _ = app
    async def _exercise() -> httpx.Response:
        async with _client_for(starlette_app) as client:
            return await client.get("/api/tools")

    response = _run(_exercise())
    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["tools"]}
    assert names == set(TOOL_NAMES)
    assert not any("execute" in n or "apply" in n for n in names)


def _client_for(starlette_app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=starlette_app),
        base_url="http://test",
    )