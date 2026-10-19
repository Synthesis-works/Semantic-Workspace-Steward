"""Live-wire integration tests: the official MCP client against the REAL M4
Streamable HTTP MCP server, running under uvicorn on a loopback ephemeral
port with the synthetic DemoBackend.

These tests prove the M5 simulator talks to the actual M4 MCP server (its
wire protocol, not a reimplementation) while staying fully local: no AWS,
no internet.

The MCP SDK requires the client session to be entered and exited in the same
event loop, so each scenario connects, runs, and closes inside one
``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Awaitable, Callable

import pytest
import uvicorn

from sws_agent.mcp.server import SwsMcpServer
from sws_agent.simulator.client import LiveMcpClient
from sws_agent.simulator.demo import DemoBackend
from sws_agent.simulator.service import Simulator

Scenario = Callable[[LiveMcpClient], Awaitable[Any]]


@pytest.fixture(scope="module")
def live_mcp_url() -> str:
    """Run the real M4 server under uvicorn and yield its MCP endpoint."""
    backend = DemoBackend()
    app = SwsMcpServer(backend=backend).streamable_http_app(path="/mcp")
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)

    def _serve() -> None:
        asyncio.run(server.serve())

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    port = None
    deadline = time.monotonic() + 5
    while port is None and time.monotonic() < deadline:
        for asgi in server.servers:
            if asgi.sockets:
                port = asgi.sockets[0].getsockname()[1]
                break
        if port is None:
            time.sleep(0.05)
    assert port is not None, "M4 loopback server did not bind a port"
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _run_with_client(url: str, scenario: Scenario) -> Any:
    async def _exercise() -> Any:
        client = LiveMcpClient(url)
        try:
            await client.connect()
            return await scenario(client)
        finally:
            await client.close()

    return asyncio.run(_exercise())


def _payload(result: Any) -> dict:
    content = result.content[0]
    structured = getattr(content, "structured_content", None)
    if structured is not None:
        return structured if isinstance(structured, dict) else json.loads(structured)
    return json.loads(content.text)


def _snapshot_args() -> dict:
    return DemoBackend().snapshot.model_dump(mode="json")


def test_client_connects_to_the_real_m4_server(live_mcp_url: str) -> None:
    async def scenario(client: LiveMcpClient) -> dict:
        return dict(client.server_info)

    info = _run_with_client(live_mcp_url, scenario)
    assert info["name"] == "sws"
    assert info["version"] == "0.2.0"


def test_tool_discovery_matches_the_real_m4_toolset(
    live_mcp_url: str,
) -> None:
    async def scenario(client: LiveMcpClient) -> list[str]:
        tools = await client.list_tools()
        return [tool["name"] for tool in tools]

    names = _run_with_client(live_mcp_url, scenario)
    assert set(names) == {
        "audit_workspace",
        "collect_workspace",
        "decide_ticket",
        "evaluate_workspace",
        "explain_resource",
        "get_cost_estimates",
        "get_relationships",
        "list_approvals",
        "request_approval",
    }
    assert len(names) == 9
    assert not any("execute" in name or "apply" in name for name in names)


def test_audit_over_wire_returns_real_structured_snapshot(
    live_mcp_url: str,
) -> None:
    async def scenario(client: LiveMcpClient) -> dict:
        result = await client.call_tool(
            "audit_workspace", {"regions": ["us-east-1"]}
        )
        assert result.is_error is False
        return _payload(result)

    payload = _run_with_client(live_mcp_url, scenario)
    assert payload["snapshot"]["snapshot_id"] == "demo-synthetic-workspace"
    assert payload["snapshot"]["partial"] is False
    assert payload["snapshot"]["truncated"] is False
    flagged = [
        decision
        for decision in payload["decisions"]
        if decision["recommended_action"] != "leave"
    ]
    assert {decision["resource_id"] for decision in flagged} == {
        "ghost-bucket-no-owner",
        "orphan-lambda-no-owner",
        "web-assets-api",
        "batch-worker-prod",
    }


def test_evaluate_and_relationships_over_wire(live_mcp_url: str) -> None:
    snapshot = _snapshot_args()

    async def scenario(client: LiveMcpClient) -> tuple[dict, dict]:
        evaluated = await client.call_tool(
            "evaluate_workspace", {"snapshot": snapshot}
        )
        related = await client.call_tool(
            "get_relationships", {"snapshot": snapshot}
        )
        return _payload(evaluated), _payload(related)

    evaluated, related = _run_with_client(live_mcp_url, scenario)
    assert evaluated["decisions"]
    assert related["relationships"]
    kinds = {r["relationship_type"] for r in related["relationships"]}
    assert "same_region" in kinds


def test_explanation_over_wire_is_demo_and_interpreted(
    live_mcp_url: str,
) -> None:
    snapshot = _snapshot_args()

    async def scenario(client: LiveMcpClient) -> dict:
        result = await client.call_tool(
            "explain_resource",
            {"snapshot": snapshot, "resource_id": "ghost-bucket-no-owner"},
        )
        assert result.is_error is False
        return _payload(result)

    payload = _run_with_client(live_mcp_url, scenario)
    assert payload["explanation"]["provider"] == "demo"
    assert payload["explanation"]["claim_kind"] == "interpreted"
    assert payload["decision"]["rule"] == "missing_owner_tag"


def test_approvals_and_decision_over_wire(live_mcp_url: str) -> None:
    async def scenario(client: LiveMcpClient) -> tuple[list, dict]:
        listed = await client.call_tool("list_approvals", {})
        before = _payload(listed)["approvals"]
        planned = await client.call_tool(
            "request_approval",
            {
                "resource_id": "web-assets-api",
                "resource_type": "ec2_instance",
                "action": "stop_resource",
                "rationale": "wire test",
            },
        )
        plan = _payload(planned)["plan"]
        decided = await client.call_tool(
            "decide_ticket",
            {
                "ticket_id": plan["ticket"]["ticket_id"],
                "decision": "grant",
                "decided_by": "demo-user",
                "reason": "demo decision",
            },
        )
        return before, _payload(decided)["ticket"]

    before, ticket = _run_with_client(live_mcp_url, scenario)
    # No tickets are seeded: an untouched demo has none, and the only ticket
    # here came from the real request_approval path.
    assert before == []
    assert ticket["resource_id"] == "web-assets-api"
    assert ticket["status"] == "granted"


def test_briefing_over_wire_returns_card_and_opens_ticket(
    live_mcp_url: str,
) -> None:
    async def scenario(client: LiveMcpClient) -> Any:
        return await Simulator(client).chat(
            "What would happen if I stopped web-assets-api?"
        )

    response = _run_with_client(live_mcp_url, scenario)
    block = response.blocks[0]
    assert block["type"] == "briefing"
    assert block["name"] == "web-assets-api"
    assert block["resource_type"] == "ec2_instance"
    assert block["state"] == "running"
    assert block["proposed_action"] == "stop_resource"
    assert block["authorization"]["requires_human_approval"] is True
    assert block["execution"] == "not_available_in_public_demo"
    assert block["ticket"]["ticket_id"]
    assert "Cloud Action Briefing" in response.reply


def test_domain_failure_shows_as_wire_is_error(live_mcp_url: str) -> None:
    async def scenario(client: LiveMcpClient) -> Any:
        return await client.call_tool(
            "decide_ticket",
            {
                "ticket_id": "no-such-ticket",
                "decision": "grant",
            },
        )

    result = _run_with_client(live_mcp_url, scenario)
    assert result.is_error is True
    assert "no-such-ticket" in result.content[0].text