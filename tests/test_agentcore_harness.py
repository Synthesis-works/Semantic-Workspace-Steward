"""M17: AgentCore Harness integration config -- hermetic boundary tests.

These tests prove, without any AWS resource, socket to AWS, or credential, that
the AgentCore Harness configuration built by ``sws_agent.agentcore_harness``
preserves the SWS boundary:

- the Harness reaches exactly the read-only SWS MCP catalog (never
  ``decide_ticket`` and no execution/mutation tool), expressed as the
  documented ``@serverName/toolName`` ``allowedTools`` patterns;
- the allowlist is checked against the **real** M4 MCP server's advertised tools
  over the wire (the same loopback MCP server the simulator tests drive);
- the remote MCP tool carries an authentication header whose value is an
  AgentCore Identity **credential reference** -- a literal credential is
  refused, so no secret can be produced by the builder;
- the declared tools are exactly one ``remote_mcp`` at an HTTPS URL -- no
  ``shell``/``file_operations``/browser/code-interpreter tool that the managed
  microVM could otherwise expose;
- memory is disabled and every execution limit is explicit;
- the module itself imports no AWS SDK and contains no mutating operation.

The live MCP server runs loopback-only under uvicorn on an ephemeral port,
exactly as ``tests/test_simulator_mcp.py`` does, so the whole file stays local.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import re
import threading
import time

import pytest
import uvicorn

from sws_agent.agentcore_harness import (
    CREDENTIAL_HEADER_VALUE_PATTERN,
    EXCLUDED_TOOL_NAMES,
    HARNESS_ALLOWED_TOOLS_MAX_ENTRIES,
    HARNESS_MAX_TOKENS,
    HARNESS_MAX_TOKENS_CEILING,
    HARNESS_MAX_ITERATIONS,
    HARNESS_MAX_ITERATIONS_CEILING,
    HARNESS_SYSTEM_PROMPT,
    HARNESS_TIMEOUT_SECONDS,
    HARNESS_TIMEOUT_SECONDS_CEILING,
    HARNESS_TOOL_NAME,
    RESERVED_HEADER_NAMES,
    allowed_tool_names,
    allowed_tools_patterns,
    build_harness_config,
    read_only_tool_names,
)
from sws_agent.mcp.server import SwsMcpServer
from sws_agent.simulator.agent import AGENT_MAX_TOKENS, AGENT_SYSTEM_PROMPT
from sws_agent.simulator.client import LiveMcpClient
from sws_agent.simulator.demo import DemoBackend

_HTTPS_URL = "https://sws-mcp.example.com/mcp"
_ROLE = "arn:aws:iam::111122223333:role/SwsHarnessRole"
_MODEL = "global.anthropic.claude-sonnet-4-6"
_CRED_REF = (
    "${arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/"
    "apikeycredentialprovider/sws-mcp-endpoint}"
)
_HEADERS = {"x-api-key": _CRED_REF}

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "sws_agent"
    / "agentcore_harness.py"
)


@pytest.fixture(scope="module")
def live_mcp_url() -> str:
    """Run the real M4 server under uvicorn and yield its MCP endpoint."""
    app = SwsMcpServer(backend=DemoBackend()).streamable_http_app(path="/mcp")
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


def _live_tool_names(url: str) -> list[str]:
    async def _scenario() -> list[str]:
        client = LiveMcpClient(url)
        try:
            await client.connect()
            tools = await client.list_tools()
            return [tool["name"] for tool in tools]
        finally:
            await client.close()

    return asyncio.run(_scenario())


def _config() -> dict:
    return build_harness_config(
        mcp_url=_HTTPS_URL,
        mcp_headers=_HEADERS,
        execution_role_arn=_ROLE,
        model_id=_MODEL,
    )


# --- Allowlist vs. the real MCP server --------------------------------------


def test_allowed_tools_are_exactly_the_read_only_agent_catalog() -> None:
    assert set(allowed_tool_names()) == set(read_only_tool_names())
    assert len(read_only_tool_names()) == 8


def test_allowed_tools_exclude_decide_ticket() -> None:
    allowed = _config()["allowedTools"]
    resolved = set(allowed_tool_names(allowed))
    assert "decide_ticket" not in resolved
    for entry in allowed:
        assert "decide_ticket" not in entry
        assert not entry.endswith("/*") and "*" not in entry
        assert entry != HARNESS_TOOL_NAME
        assert not entry.startswith("@builtin")
    for excluded in EXCLUDED_TOOL_NAMES:
        assert excluded not in resolved


def test_allowed_tools_use_documented_server_scoped_patterns() -> None:
    allowed = _config()["allowedTools"]
    pattern = re.compile(r"(\*|@?[^/]+(/[^/]+)?)")
    assert allowed == allowed_tools_patterns()
    assert 1 <= len(allowed) <= HARNESS_ALLOWED_TOOLS_MAX_ENTRIES
    for name in read_only_tool_names():
        assert f"@{HARNESS_TOOL_NAME}/{name}" in allowed, name
        assert name in allowed, name
    for entry in allowed:
        assert pattern.fullmatch(entry), entry
        assert len(entry.split("/")) <= 2, entry


def test_allowed_tools_are_served_by_the_real_mcp_server(
    live_mcp_url: str,
) -> None:
    served = set(_live_tool_names(live_mcp_url))
    allowed = set(allowed_tool_names(_config()["allowedTools"]))
    assert allowed <= served, f"allowlist not served by MCP: {allowed - served}"


def test_allowlist_is_exactly_the_real_read_only_tools(
    live_mcp_url: str,
) -> None:
    served = set(_live_tool_names(live_mcp_url))
    allowed = set(allowed_tool_names(_config()["allowedTools"]))
    assert allowed == served - EXCLUDED_TOOL_NAMES


def test_mcp_surface_is_unchanged_at_nine_tools(live_mcp_url: str) -> None:
    names = _live_tool_names(live_mcp_url)
    assert len(names) == 9
    assert "decide_ticket" in names


def test_allowlist_names_no_execution_or_mutation_tool() -> None:
    forbidden_fragments = ("execute", "apply", "stop", "delete", "terminate", "mutate")
    for name in allowed_tool_names(_config()["allowedTools"]):
        assert not any(fragment in name.lower() for fragment in forbidden_fragments)


# --- Declared tools: exactly one remote_mcp, no managed-loop tool -----------


def test_config_declares_only_the_remote_mcp_tool() -> None:
    tools = _config()["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    tool = tools[0]
    assert tool["type"] == "remote_mcp"
    remote = tool["config"]["remoteMcp"]
    assert remote["url"] == _HTTPS_URL
    assert remote["headers"] == _HEADERS


def test_config_declares_no_builtin_or_agentcore_tool() -> None:
    declared = str(_config()["tools"]).lower()
    for forbidden in (
        "shell",
        "file_operations",
        "code_interpreter",
        "browser",
        "agentcore_gateway",
    ):
        assert forbidden not in declared


# --- Memory and limits ------------------------------------------------------


def test_memory_is_disabled() -> None:
    assert _config()["memory"] == {"disabled": {}}


def test_execution_limits_are_explicit_and_bounded() -> None:
    config = _config()
    assert config["maxIterations"] == HARNESS_MAX_ITERATIONS
    assert config["maxTokens"] == HARNESS_MAX_TOKENS
    assert config["timeoutSeconds"] == HARNESS_TIMEOUT_SECONDS
    assert 1 <= HARNESS_MAX_ITERATIONS <= HARNESS_MAX_ITERATIONS_CEILING
    assert 1 <= HARNESS_MAX_TOKENS <= HARNESS_MAX_TOKENS_CEILING
    assert 1 <= HARNESS_TIMEOUT_SECONDS <= HARNESS_TIMEOUT_SECONDS_CEILING


def test_model_generation_ceiling_is_explicit() -> None:
    model = _config()["model"]["bedrockModelConfig"]
    assert model["maxTokens"] == AGENT_MAX_TOKENS
    assert model["apiFormat"] == "converse_stream"


def test_config_is_deterministic() -> None:
    assert _config() == _config()


# --- Input validation -------------------------------------------------------


def test_mcp_url_must_be_https() -> None:
    with pytest.raises(ValueError):
        build_harness_config(
            mcp_url="http://sws-mcp.example.com/mcp",
            mcp_headers=_HEADERS,
            execution_role_arn=_ROLE,
            model_id=_MODEL,
        )
    with pytest.raises(ValueError):
        build_harness_config(
            mcp_url="",
            mcp_headers=_HEADERS,
            execution_role_arn=_ROLE,
            model_id=_MODEL,
        )


def test_execution_role_and_model_are_required() -> None:
    with pytest.raises(ValueError):
        build_harness_config(
            mcp_url=_HTTPS_URL,
            mcp_headers=_HEADERS,
            execution_role_arn="  ",
            model_id=_MODEL,
        )
    with pytest.raises(ValueError):
        build_harness_config(
            mcp_url=_HTTPS_URL,
            mcp_headers=_HEADERS,
            execution_role_arn=_ROLE,
            model_id="",
        )


def test_harness_name_must_satisfy_agentcore_constraints() -> None:
    for bad in ("1abc", "_abc", "a" * 41, "has-dash", "has space", ""):
        with pytest.raises(ValueError):
            build_harness_config(
                mcp_url=_HTTPS_URL,
                mcp_headers=_HEADERS,
                execution_role_arn=_ROLE,
                model_id=_MODEL,
                harness_name=bad,
            )
    assert build_harness_config(
        mcp_url=_HTTPS_URL,
        mcp_headers=_HEADERS,
        execution_role_arn=_ROLE,
        model_id=_MODEL,
        harness_name="Sws_Harness_01",
    )["harnessName"] == "Sws_Harness_01"


def test_limits_above_ceiling_are_refused() -> None:
    with pytest.raises(ValueError):
        build_harness_config(
            mcp_url=_HTTPS_URL,
            mcp_headers=_HEADERS,
            execution_role_arn=_ROLE,
            model_id=_MODEL,
            max_iterations=HARNESS_MAX_ITERATIONS_CEILING + 1,
        )
    with pytest.raises(ValueError):
        build_harness_config(
            mcp_url=_HTTPS_URL,
            mcp_headers=_HEADERS,
            execution_role_arn=_ROLE,
            model_id=_MODEL,
            max_tokens=HARNESS_MAX_TOKENS_CEILING + 1,
        )
    with pytest.raises(ValueError):
        build_harness_config(
            mcp_url=_HTTPS_URL,
            mcp_headers=_HEADERS,
            execution_role_arn=_ROLE,
            model_id=_MODEL,
            timeout_seconds=HARNESS_TIMEOUT_SECONDS_CEILING + 1,
        )


# --- Authentication: credential references only, never a secret --------------


def test_authentication_headers_are_required() -> None:
    for bad in (None, {}, {"x-api-key": ""}):
        with pytest.raises(ValueError):
            build_harness_config(
                mcp_url=_HTTPS_URL,
                mcp_headers=bad,
                execution_role_arn=_ROLE,
                model_id=_MODEL,
            )


def test_credential_reference_header_round_trips_unchanged() -> None:
    config = build_harness_config(
        mcp_url=_HTTPS_URL,
        mcp_headers={"Authorization": _CRED_REF, "x-api-key": _CRED_REF},
        execution_role_arn=_ROLE,
        model_id=_MODEL,
    )
    headers = config["tools"][0]["config"]["remoteMcp"]["headers"]
    assert headers == {"Authorization": _CRED_REF, "x-api-key": _CRED_REF}


def test_literal_credentials_in_header_values_are_refused() -> None:
    literals = (
        "Bearer some-token-value",
        "some-token-value",
        _CRED_REF + " ",
        "x" + _CRED_REF,
        _CRED_REF[:-1],
        "${arn:aws:s3:::not-a-credential-provider}",
        "${arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/"
        "secretsmanager/something}",
        "",
    )
    for value in literals:
        with pytest.raises(ValueError):
            build_harness_config(
                mcp_url=_HTTPS_URL,
                mcp_headers={"x-api-key": value},
                execution_role_arn=_ROLE,
                model_id=_MODEL,
            )


def test_malformed_and_reserved_header_names_are_refused() -> None:
    for name in ("bad header", "bad:header", "", "x" * 16384, "x-api\nkey"):
        with pytest.raises(ValueError):
            build_harness_config(
                mcp_url=_HTTPS_URL,
                mcp_headers={name: _CRED_REF},
                execution_role_arn=_ROLE,
                model_id=_MODEL,
            )
    for name in RESERVED_HEADER_NAMES:
        with pytest.raises(ValueError):
            build_harness_config(
                mcp_url=_HTTPS_URL,
                mcp_headers={name: _CRED_REF},
                execution_role_arn=_ROLE,
                model_id=_MODEL,
            )


def test_oversized_header_value_is_refused() -> None:
    oversized = (
        "${arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/"
        "apikeycredentialprovider/" + "a" * 16400 + "}"
    )
    assert len(oversized) > 16383
    with pytest.raises(ValueError):
        build_harness_config(
            mcp_url=_HTTPS_URL,
            mcp_headers={"x-api-key": oversized},
            execution_role_arn=_ROLE,
            model_id=_MODEL,
        )


def test_generated_configuration_never_contains_a_credential() -> None:
    headers = _config()["tools"][0]["config"]["remoteMcp"]["headers"]
    assert headers, "the emitted configuration carries no credential reference"
    for name, value in headers.items():
        assert CREDENTIAL_HEADER_VALUE_PATTERN.fullmatch(value), name
    assert "${arn:aws:bedrock-agentcore:" in json.dumps(_config())


# --- Boundary prompt --------------------------------------------------------


def test_harness_prompt_carries_the_boundary_rules() -> None:
    for marker in (
        "UNTRUSTED DATA",
        "Never invent costs",
        "capability is not available",
        "Do NOT infer, rank, or compare risk",
        "Do not reference any other command",
        "Approval decisions (grant/deny) are NOT available to you",
    ):
        assert marker in HARNESS_SYSTEM_PROMPT, marker


def test_harness_and_local_prompts_share_the_boundary_rules() -> None:
    shared = (
        "UNTRUSTED DATA",
        "Never invent costs",
        "capability is not available",
        "Do NOT infer, rank, or compare risk",
        "Do not reference any other command",
        "Approval decisions (grant/deny) are NOT available to you",
    )
    for marker in shared:
        assert marker in AGENT_SYSTEM_PROMPT, f"local prompt lost rule: {marker}"
        assert marker in HARNESS_SYSTEM_PROMPT, f"harness prompt lost rule: {marker}"


def test_harness_prompt_is_emitted_as_content_blocks() -> None:
    prompt = _config()["systemPrompt"]
    assert isinstance(prompt, list) and len(prompt) == 1
    assert prompt[0]["text"] == HARNESS_SYSTEM_PROMPT


# --- The module itself holds no authority -----------------------------------


def test_module_imports_no_aws_sdk_and_has_no_mutating_call() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "boto3" not in imported
    assert "botocore" not in imported
    assert "bedrock_agentcore" not in imported

    source = _MODULE_PATH.read_text(encoding="utf-8")
    for mutation in ("stop_instances", "terminate_instances", "create_harness("):
        assert mutation not in source
