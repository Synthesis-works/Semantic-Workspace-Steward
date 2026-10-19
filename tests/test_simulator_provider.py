"""Hermetic tests for the public-demo conversational provider selection.

These tests never construct a real Bedrock client and never touch the network,
AWS, or credentials: the Strands constructors are monkeypatched and all
simulator behavior is driven through in-process fakes. A separate, manual
rehearsal exercises the real model (see the runbook).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from sws_agent.simulator import agent as agent_mod
from sws_agent.simulator.app import create_app
from sws_agent.simulator.service import Simulator

REPO = Path(__file__).resolve().parents[1]

_AGENT_ENV = (
    "SWS_AGENT_MODEL",
    "SWS_AGENT_PROVIDER",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


def _clear_agent_env(monkeypatch) -> None:
    for name in _AGENT_ENV:
        monkeypatch.delenv(name, raising=False)


# --- provider selection ------------------------------------------------------


def test_no_model_means_deterministic(monkeypatch) -> None:
    _clear_agent_env(monkeypatch)
    assert agent_mod.agent_from_env() is None


def test_model_with_default_provider_selects_strands(monkeypatch) -> None:
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("SWS_AGENT_MODEL", "amazon.nova-lite-v1:0")
    got = agent_mod.agent_from_env()
    assert isinstance(got, agent_mod.StrandsConversationAgent)
    assert got.provider == "strands"
    assert got.model == "amazon.nova-lite-v1:0"


def test_explicit_strands_provider_selects_strands(monkeypatch) -> None:
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("SWS_AGENT_MODEL", "amazon.nova-lite-v1:0")
    monkeypatch.setenv("SWS_AGENT_PROVIDER", "strands")
    assert isinstance(
        agent_mod.agent_from_env(), agent_mod.StrandsConversationAgent
    )


def test_unknown_provider_stays_deterministic(monkeypatch) -> None:
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("SWS_AGENT_MODEL", "amazon.nova-lite-v1:0")
    monkeypatch.setenv("SWS_AGENT_PROVIDER", "something-else")
    assert agent_mod.agent_from_env() is None


def test_blank_model_is_deterministic(monkeypatch) -> None:
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("SWS_AGENT_MODEL", "   ")
    assert agent_mod.agent_from_env() is None


# --- region resolution -------------------------------------------------------


def test_region_defaults_to_us_east_1(monkeypatch) -> None:
    _clear_agent_env(monkeypatch)
    assert agent_mod._resolve_region() == "us-east-1"


def test_region_prefers_aws_region(monkeypatch) -> None:
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert agent_mod._resolve_region() == "us-west-2"


def test_region_falls_back_to_default_region(monkeypatch) -> None:
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    assert agent_mod._resolve_region() == "eu-west-1"


def test_agent_from_env_passes_selected_region(monkeypatch) -> None:
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("SWS_AGENT_MODEL", "amazon.nova-lite-v1:0")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    got = agent_mod.agent_from_env()
    assert isinstance(got, agent_mod.StrandsConversationAgent)
    assert got.region == "us-west-2"


# --- cost ceilings -----------------------------------------------------------


def test_cost_ceilings_are_bounded() -> None:
    assert agent_mod.AGENT_MAX_TOKENS == 768
    assert agent_mod.AGENT_MAX_STEPS <= 6
    assert agent_mod.AGENT_HISTORY_LIMIT <= 16


def test_provider_labels_map_strands_to_bedrock() -> None:
    assert agent_mod.AGENT_PROVIDER_LABELS["strands"] == "Amazon Bedrock"


def test_model_invoker_configures_bedrock_with_bounds(monkeypatch) -> None:
    import types

    captured: dict[str, Any] = {}

    class FakeBedrockModel:
        def __init__(self, **kwargs: Any) -> None:
            captured["bedrock"] = kwargs

    class FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["agent"] = kwargs

    class FakeNullConversationManager:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class FakeMaxTokensReached(Exception):
        pass

    # Inject fake Strands modules so this unit test never imports the real
    # package (which would pull boto3 transitively and defeat the
    # no-AWS-resource-SDK guarantee asserted elsewhere).
    fake_strands = types.ModuleType("strands")
    fake_strands.Agent = FakeAgent
    fake_models = types.ModuleType("strands.models")
    fake_models.BedrockModel = FakeBedrockModel
    fake_agent_pkg = types.ModuleType("strands.agent")
    fake_conversation_manager = types.ModuleType(
        "strands.agent.conversation_manager"
    )
    fake_conversation_manager.NullConversationManager = (
        FakeNullConversationManager
    )
    fake_types = types.ModuleType("strands.types")
    fake_exceptions = types.ModuleType("strands.types.exceptions")
    fake_exceptions.MaxTokensReachedException = FakeMaxTokensReached
    for name, module in (
        ("strands", fake_strands),
        ("strands.models", fake_models),
        ("strands.agent", fake_agent_pkg),
        ("strands.agent.conversation_manager", fake_conversation_manager),
        ("strands.types", fake_types),
        ("strands.types.exceptions", fake_exceptions),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    agent_mod._model_invoker("amazon.nova-lite-v1:0", region_name="us-east-1")

    assert captured["bedrock"]["model_id"] == "amazon.nova-lite-v1:0"
    assert captured["bedrock"]["max_tokens"] == agent_mod.AGENT_MAX_TOKENS
    assert captured["bedrock"]["region_name"] == "us-east-1"
    # Non-streaming: Strands must call Converse, not ConverseStream, so the
    # runtime needs only bedrock:InvokeModel (no InvokeModelWithResponseStream).
    assert captured["bedrock"]["streaming"] is False
    # No retries (no cost-multiplying retry storms), no tools, no streaming.
    assert captured["agent"]["tools"] == []
    assert captured["agent"]["retry_strategy"] is None
    assert captured["agent"]["callback_handler"] is None


# --- fakes -------------------------------------------------------------------


class FakeMcpClient:
    SNAPSHOT = {
        "snapshot_id": "demo-synthetic-workspace",
        "regions": ["us-east-1"],
        "counts": {"s3_bucket": 2},
        "resources": [
            {
                "resource_id": "ghost-bucket-no-owner",
                "name": "ghost-bucket-no-owner",
                "resource_type": "s3_bucket",
                "region": "us-east-1",
                "owner_tag": None,
            }
        ],
    }
    DECISIONS = [
        {
            "resource_id": "ghost-bucket-no-owner",
            "recommended_action": "flag_for_review",
            "risk_level": "medium",
            "rationale": "no owner tag",
            "rule": "missing_owner_tag",
            "confidence": 1.0,
        }
    ]

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def list_tools(self) -> list[dict[str, str]]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        if name in ("audit_workspace", "collect_workspace"):
            payload = {"snapshot": self.SNAPSHOT, "decisions": self.DECISIONS}
        elif name == "explain_resource":
            payload = {
                "explanation": {"text": "demo explanation", "provider": "demo"},
                "decision": self.DECISIONS[0],
            }
        elif name == "request_approval":
            payload = {
                "plan": {
                    "resource_id": arguments.get("resource_id"),
                    "action": arguments.get("action"),
                    "execution_mode": "safe",
                    "authorization": {
                        "decision": "pending_approval",
                        "requires_human_approval": True,
                    },
                    "ticket": {
                        "ticket_id": "demo-ticket-0001",
                        "status": "pending",
                    },
                    "executed": False,
                }
            }
        elif name == "decide_ticket":
            payload = {
                "ticket": {
                    "ticket_id": arguments.get("ticket_id", "-"),
                    "status": "granted",
                }
            }
        else:
            payload = {"name": name}
        return SimpleNamespace(
            is_error=False,
            content=[
                SimpleNamespace(
                    text=json.dumps(payload), structured_content=None
                )
            ],
        )


class ScriptedAgent:
    """ConversationAgent returning pre-queued raw strings (no Strands)."""

    provider = "strands"
    model = "amazon.nova-lite-v1:0"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self._index = 0

    def respond(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self._index >= len(self.responses):
            raise agent_mod.AgentError("no scripted response remaining")
        resp = self.responses[self._index]
        self._index += 1
        return resp


class UnavailableAgent:
    provider = "strands"
    model = "amazon.nova-lite-v1:0"

    def respond(self, prompt: str) -> str:
        raise agent_mod.AgentUnavailableError("the package is not installed")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _meta(starlette_app: Any) -> dict[str, Any]:
    async def _exercise() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=starlette_app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/meta")
            return response.json()

    return _run(_exercise())


# --- routing: explicit approve/deny stays deterministic ----------------------


def test_explicit_approve_uses_deterministic_path_not_the_model() -> None:
    client = FakeMcpClient()
    agent_fake = ScriptedAgent([])  # would raise if the model were consulted
    sim = Simulator(client, agent=agent_fake)
    resp = _run(sim.chat("Approve this ticket demo-ticket-0001"))
    assert client.calls and client.calls[0][0] == "decide_ticket"
    assert client.calls[0][1]["ticket_id"] == "demo-ticket-0001"
    assert client.calls[0][1]["decision"] == "grant"
    assert agent_fake.prompts == []
    assert "No AWS action was executed" in resp.reply


def test_explicit_deny_uses_deterministic_path_not_the_model() -> None:
    client = FakeMcpClient()
    agent_fake = ScriptedAgent([])
    sim = Simulator(client, agent=agent_fake)
    _run(sim.chat("Deny demo-ticket-0002"))
    assert client.calls[0][0] == "decide_ticket"
    assert client.calls[0][1]["decision"] == "deny"
    assert agent_fake.prompts == []


def test_briefing_stays_deterministic_and_never_consults_the_model() -> None:
    client = FakeMcpClient()
    agent_fake = ScriptedAgent([])  # would raise if the model were consulted
    sim = Simulator(client, agent=agent_fake)
    resp = _run(
        sim.chat("What would happen if I stopped ghost-bucket-no-owner?")
    )
    assert agent_fake.prompts == []
    assert [name for name, _ in client.calls] == [
        "audit_workspace",
        "explain_resource",
    ]
    assert resp.blocks and resp.blocks[0]["type"] == "explanation"


def test_free_form_text_reaches_the_agent_loop() -> None:
    client = FakeMcpClient()
    agent_fake = ScriptedAgent(
        [
            '{"tool": "audit_workspace", "arguments": {}}',
            '{"answer": "All set."}',
        ]
    )
    sim = Simulator(client, agent=agent_fake)
    resp = _run(sim.chat("tell me about the workspace"))
    assert client.calls and client.calls[0][0] == "audit_workspace"
    assert len(agent_fake.prompts) == 2
    assert resp.reply.startswith("All set.")


def test_agent_proposed_decide_is_refused_and_never_called() -> None:
    client = FakeMcpClient()
    agent_fake = ScriptedAgent(
        ['{"tool": "decide_ticket", "arguments": {"ticket_id": "demo-ticket-0001"}}']
    )
    sim = Simulator(client, agent=agent_fake)
    resp = _run(sim.chat("go ahead and take care of that outstanding item"))
    assert client.calls == []
    assert "refused" in resp.reply.lower()


def test_bedrock_unavailable_degrades_honestly() -> None:
    client = FakeMcpClient()
    sim = Simulator(client, agent=UnavailableAgent())
    resp = _run(sim.chat("hello there"))
    assert "conversational model is unavailable" in resp.reply
    assert resp.blocks and resp.blocks[0]["type"] == "error"
    assert any(
        e["tool"] == "agent" and e["state"] == "error" for e in resp.trace
    )
    # Never fabricate tool activity the model did not trigger.
    assert client.calls == []


# --- metadata / UI honesty ---------------------------------------------------


def test_meta_reports_provider_when_agent_configured() -> None:
    sim = Simulator(FakeMcpClient(), agent=ScriptedAgent([]))
    meta = _meta(create_app(simulator=sim))
    assert meta["mode"] == "llm"
    assert meta["provider"] == "strands"
    assert meta["provider_label"] == "Amazon Bedrock"
    assert meta["model"] == "amazon.nova-lite-v1:0"
    assert meta["synthetic"] is True


def test_meta_reports_deterministic_without_agent() -> None:
    sim = Simulator(FakeMcpClient())
    meta = _meta(create_app(simulator=sim))
    assert meta["mode"] == "deterministic"
    assert meta["provider"] is None
    assert meta["provider_label"] is None
    assert meta["model"] is None


def test_default_mcp_endpoint_is_loopback() -> None:
    sim = Simulator(FakeMcpClient())
    meta = _meta(create_app(simulator=sim))
    assert meta["mcp_endpoint"].startswith("http://127.0.0.1:")


# --- dependency / boundary guardrails ----------------------------------------


def test_simulator_import_pulls_no_aws_resource_sdk() -> None:
    code = (
        "import sys;"
        "import sws_agent.simulator, sws_agent.simulator.agent,"
        " sws_agent.simulator.service, sws_agent.simulator.app;"
        "print('BOTO3' if ('boto3' in sys.modules"
        " or 'sws_agent.aws' in sys.modules) else 'CLEAN')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "CLEAN"


def test_render_manifest_enables_real_provider_without_live_mcp() -> None:
    text = (REPO / "render.yaml").read_text(encoding="utf-8")
    assert "SWS_AGENT_PROVIDER" in text
    assert "SWS_AGENT_MODEL" in text
    assert "amazon.nova-lite-v1:0" in text
    assert "AWS_REGION" in text
    # The public demo must never point at the live MCP deployment.
    assert "SWS_MCP_URL" not in text
    assert "mcp.sjsujalgupta.xyz" not in text


def test_web_dockerfile_installs_conversational_extra_and_no_secrets() -> None:
    text = (REPO / "deploy" / "web" / "Dockerfile").read_text(encoding="utf-8")
    assert ".[simulator,explanation]" in text
    assert "AWS_ACCESS_KEY_ID" not in text
    assert "AWS_SECRET_ACCESS_KEY" not in text
    assert "AKIA" not in text
    assert "SWS_MCP_URL" not in text


def test_explanation_extra_declares_strands() -> None:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "strands-agents" in text


def test_ui_states_provider_honestly_for_each_mode() -> None:
    static = REPO / "src" / "sws_agent" / "simulator" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    # The footer must be a live element the client can rewrite, not a static
    # claim, so the honesty text always matches the mode actually reported by
    # /api/meta (llm vs deterministic).
    assert 'id="demo-footer"' in html
    assert "demo-footer" in js
    assert "meta.mode === \"llm\"" in js
    # Deterministic fallback must not claim an LLM answered.
    assert "no LLM" in js
