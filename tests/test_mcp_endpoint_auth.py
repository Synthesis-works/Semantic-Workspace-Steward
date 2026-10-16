"""M17: the opt-in caller-authentication gate for the deployed MCP endpoint.

Hermetic: no AWS, no internet. Secrets Manager is never called -- the reader
is injected (unit tests) or monkeypatched (integration test), which is exactly
the seam ``endpoint_auth`` was built with.

The integration test serves the REAL M4 Streamable HTTP app on loopback and
drives it with the same JSON-RPC handshake the AgentCore Harness performs, so
the header the harness sends is proven to satisfy the gate end to end.
"""

from __future__ import annotations

import ast
import asyncio
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from sws_agent.mcp import endpoint_auth
from sws_agent.mcp.server import SwsMcpServer
from sws_agent.simulator.demo import DemoBackend

SECRET_ARN = (
    "arn:aws:secretsmanager:us-east-1:527557823928:secret:sws/mcp-endpoint-AbCdEf"
)
API_KEY = "example-endpoint-api-key"
FOREIGN_HOST = "mcp.sws.example.com"


@pytest.fixture(autouse=True)
def _clear_endpoint_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is opt-in; no test may depend on an ambient configuration."""
    monkeypatch.delenv(
        endpoint_auth.SWS_MCP_API_KEY_SECRET_ARN_ENV, raising=False
    )
    monkeypatch.delenv(endpoint_auth.SWS_MCP_AUTH_HEADER_ENV, raising=False)


@contextmanager
def _serve(app: Any):
    """Serve ``app`` on a loopback ephemeral port; yield the base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve()), daemon=True
    )
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
    assert port is not None, "loopback server did not bind a port"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "sws-endpoint-auth-test", "version": "0"},
    },
}


def _initialize(base: str, *, headers: dict[str, str] | None = None):
    return httpx.post(
        base + "/mcp",
        json=_INITIALIZE,
        headers={
            "accept": "application/json, text/event-stream",
            **(headers or {}),
        },
        timeout=10.0,
    )


# --------------------------------------------------------------------------
# Secret parsing: pure functions, no AWS.
# --------------------------------------------------------------------------


def test_api_key_is_read_from_the_json_field_and_trimmed():
    assert endpoint_auth.api_key_from_secret_string(
        json.dumps({"api_key": f"  {API_KEY}\n"})
    ) == API_KEY


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '"a string"',
        "{}",
        '{"api_key": ""}',
        '{"api_key": "   "}',
        '{"api_key": 7}',
        '{"api-key": "wrong field name"}',
    ],
)
def test_unusable_secret_bodies_are_rejected(raw: str):
    with pytest.raises(ValueError):
        endpoint_auth.api_key_from_secret_string(raw)


def test_secret_contents_never_appear_in_the_error():
    with pytest.raises(ValueError) as exc:
        endpoint_auth.api_key_from_secret_string('{"apikey": "leaked-value"}')
    assert "leaked-value" not in str(exc.value)


@pytest.mark.parametrize(
    "arn, expected",
    [
        (SECRET_ARN, "us-east-1"),
        ("arn:aws:secretsmanager:eu-west-2:123456789012:secret:n-abcdef",
         "eu-west-2"),
        ("arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:n-a", "cn-north-1"),
        ("sws/plain-name", None),
        ("", None),
    ],
)
def test_region_is_recovered_from_the_secret_arn(arn: str, expected):
    assert endpoint_auth.region_from_secret_arn(arn) == expected


# --------------------------------------------------------------------------
# Opt-in resolution: unconfigured is a true no-op; misconfigured aborts.
# --------------------------------------------------------------------------


def test_unconfigured_endpoint_requires_no_credential():
    assert endpoint_auth.credential_from_environment(environ={}) is None
    assert endpoint_auth.credential_from_environment(
        environ={endpoint_auth.SWS_MCP_API_KEY_SECRET_ARN_ENV: "   "}
    ) is None


def test_gate_returns_the_same_app_when_unconfigured():
    app = object()
    assert endpoint_auth.gate(app, environ={}) is app
    assert endpoint_auth.gate(app, environ={}, api_key_reader=_boom) is app


def test_configured_endpoint_uses_the_documented_default_header():
    credential = endpoint_auth.credential_from_environment(
        environ={endpoint_auth.SWS_MCP_API_KEY_SECRET_ARN_ENV: SECRET_ARN},
        api_key_reader=lambda arn: API_KEY,
    )
    assert credential is not None
    assert credential.header_name == endpoint_auth.DEFAULT_AUTH_HEADER == "x-api-key"
    assert credential.api_key == API_KEY


def test_header_name_can_be_overridden_and_is_normalized():
    credential = endpoint_auth.credential_from_environment(
        environ={
            endpoint_auth.SWS_MCP_API_KEY_SECRET_ARN_ENV: SECRET_ARN,
            endpoint_auth.SWS_MCP_AUTH_HEADER_ENV: "X-SWS-Key",
        },
        api_key_reader=lambda arn: API_KEY,
    )
    assert credential is not None
    assert credential.header_name == "x-sws-key"


def test_invalid_header_name_aborts_startup():
    with pytest.raises(ValueError):
        endpoint_auth.credential_from_environment(
            environ={
                endpoint_auth.SWS_MCP_API_KEY_SECRET_ARN_ENV: SECRET_ARN,
                endpoint_auth.SWS_MCP_AUTH_HEADER_ENV: "not a header name",
            },
            api_key_reader=lambda arn: API_KEY,
        )


@pytest.mark.parametrize("resolved", ["", "   ", None])
def test_empty_resolved_key_aborts_startup(resolved: Any):
    with pytest.raises(ValueError):
        endpoint_auth.credential_from_environment(
            environ={endpoint_auth.SWS_MCP_API_KEY_SECRET_ARN_ENV: SECRET_ARN},
            api_key_reader=lambda arn: resolved,
        )


def test_unreadable_secret_aborts_startup_instead_of_opening_the_gate():
    with pytest.raises(RuntimeError):
        endpoint_auth.credential_from_environment(
            environ={endpoint_auth.SWS_MCP_API_KEY_SECRET_ARN_ENV: SECRET_ARN},
            api_key_reader=_boom,
        )


def _boom(arn: str) -> str:
    raise RuntimeError(f"could not read {arn}")


def test_the_credential_never_appears_in_its_repr():
    credential = endpoint_auth.EndpointCredential(
        header_name="x-api-key", api_key=API_KEY
    )
    assert API_KEY not in repr(credential)


# --------------------------------------------------------------------------
# ASGI gate behavior, in process (no port).
# --------------------------------------------------------------------------


class _Recorder:
    """Minimal ASGI app that records how many times it was reached."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope, receive, send) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _call(
    app: Any,
    headers: list[tuple[bytes, bytes]],
    *,
    scope_type: str = "http",
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    scope = {
        "type": scope_type,
        "method": "POST",
        "path": "/mcp",
        "query_string": b"",
        "scheme": "http",
        "headers": headers,
    }
    asyncio.run(app(scope, receive, send))
    return sent


def _status(sent: list[dict[str, Any]]) -> int:
    return sent[0]["status"]


@pytest.fixture
def gated():
    inner = _Recorder()
    app = endpoint_auth.require_credential(
        inner,
        credential=endpoint_auth.EndpointCredential(
            header_name="x-api-key", api_key=API_KEY
        ),
    )
    return app, inner


def test_request_without_the_credential_is_refused_before_the_app(gated):
    app, inner = gated
    sent = _call(app, [(b"content-type", b"application/json")])
    assert _status(sent) == 401
    assert inner.calls == 0


def test_wrong_credential_is_refused(gated):
    app, inner = gated
    sent = _call(app, [(b"x-api-key", b"not-the-key")])
    assert _status(sent) == 401
    assert inner.calls == 0


@pytest.mark.parametrize(
    "headers",
    [
        [(b"x-api-key", API_KEY.encode())],
        [(b"X-Api-Key", API_KEY.encode())],
        [(b"x-api-key", f"  {API_KEY}".encode())],
        [(b"x-api-key", b"other"), (b"x-api-key", API_KEY.encode())],
        [(b"host", b"mcp.sws.example.com"), (b"x-api-key", API_KEY.encode())],
    ],
)
def test_the_configured_credential_is_accepted(gated, headers):
    app, inner = gated
    sent = _call(app, headers)
    assert _status(sent) == 200
    assert inner.calls == 1


def test_a_prefix_of_the_credential_is_not_accepted(gated):
    app, inner = gated
    sent = _call(app, [(b"x-api-key", API_KEY[:-1].encode())])
    assert _status(sent) == 401
    assert inner.calls == 0


def test_the_refusal_echoes_nothing_from_the_request(gated):
    app, _ = gated
    sent = _call(app, [(b"x-api-key", b"attacker-supplied-value")])
    body = sent[1]["body"]
    assert body == b'{"error":"unauthorized"}'
    assert API_KEY.encode() not in body
    assert b"attacker-supplied-value" not in body


def test_non_http_scopes_pass_through(gated):
    app, inner = gated
    _call(app, [], scope_type="lifespan")
    assert inner.calls == 1


# --------------------------------------------------------------------------
# The real server: env-driven wiring and the loopback handshake.
# --------------------------------------------------------------------------


def test_enabled_gate_makes_the_real_server_refuse_anonymous_requests(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(endpoint_auth.SWS_MCP_API_KEY_SECRET_ARN_ENV, SECRET_ARN)
    monkeypatch.setattr(
        endpoint_auth, "read_api_key_from_secrets_manager", lambda arn: API_KEY
    )
    app = SwsMcpServer(backend=DemoBackend()).streamable_http_app(path="/mcp")
    with _serve(app) as base:
        anonymous = _initialize(base)
        assert anonymous.status_code == 401
        assert anonymous.text == '{"error":"unauthorized"}'
        assert API_KEY not in anonymous.text


def test_the_harness_handshake_succeeds_with_the_configured_header(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(endpoint_auth.SWS_MCP_API_KEY_SECRET_ARN_ENV, SECRET_ARN)
    monkeypatch.setattr(
        endpoint_auth, "read_api_key_from_secrets_manager", lambda arn: API_KEY
    )
    app = SwsMcpServer(backend=DemoBackend()).streamable_http_app(path="/mcp")
    with _serve(app) as base:
        response = _initialize(base, headers={"x-api-key": API_KEY})
        assert response.status_code == 200
        assert response.headers.get("mcp-session-id")


def test_the_server_is_unauthenticated_when_the_variable_is_absent():
    app = SwsMcpServer(backend=DemoBackend()).streamable_http_app(path="/mcp")
    with _serve(app) as base:
        assert _initialize(base).status_code == 200


def test_run_serves_the_gated_application_factory(
    monkeypatch: pytest.MonkeyPatch,
):
    """``run()`` must build its app through ``streamable_http_app()``.

    Otherwise the SDK's own runner would build an ungated application and the
    credential check could be bypassed by starting the server the documented
    way.
    """

    class _Stop(Exception):
        pass

    server = SwsMcpServer(backend=DemoBackend())
    seen: dict[str, Any] = {}

    def fake_app(*, path: str = "/mcp", host: str = "127.0.0.1"):
        seen["path"] = path
        seen["host"] = host
        raise _Stop

    monkeypatch.setattr(server, "streamable_http_app", fake_app)
    with pytest.raises(_Stop):
        server.run(host="0.0.0.0", port=8123, path="/custom")
    assert seen == {"path": "/custom", "host": "0.0.0.0"}


# --------------------------------------------------------------------------
# Transport: which Host the deployed endpoint accepts (measured behavior).
# --------------------------------------------------------------------------


def test_deploy_host_forwarding_accepts_the_load_balancer_host_header():
    app = SwsMcpServer(backend=DemoBackend()).streamable_http_app(
        path="/mcp", host="0.0.0.0"
    )
    with _serve(app) as base:
        response = httpx.get(
            base + "/mcp", headers={"Host": FOREIGN_HOST}, timeout=10.0
        )
        assert response.status_code == 400, response.text
        assert "Host header" not in response.text


def test_loopback_build_rejects_a_foreign_host_header():
    app = SwsMcpServer(backend=DemoBackend()).streamable_http_app(path="/mcp")
    with _serve(app) as base:
        response = httpx.get(
            base + "/mcp", headers={"Host": FOREIGN_HOST}, timeout=10.0
        )
        assert response.status_code == 421


# --------------------------------------------------------------------------
# Hygiene: stdlib-only import surface, lazy boto3, constant-time compare.
# --------------------------------------------------------------------------


def _source() -> str:
    return Path(endpoint_auth.__file__).read_text(encoding="utf-8")


def _top_level_imports(source: str) -> list[str]:
    names: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_module_level_imports_never_include_boto3():
    imports = _top_level_imports(_source())
    assert not [name for name in imports if name.startswith("boto")]
    assert "hmac" in imports
    assert "json" in imports
    assert "os" in imports


def test_boto3_is_imported_only_inside_the_reader():
    tree = ast.parse(_source())
    lazy: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Import):
                    lazy.extend(alias.name for alias in inner.names)
    assert "boto3" in lazy


def test_comparison_is_constant_time_and_never_printed():
    source = _source()
    assert "hmac.compare_digest(" in source
    assert "print(" not in source


def test_importing_the_module_does_not_import_boto3():
    import subprocess
    import sys

    code = (
        "import sys; import sws_agent.mcp.endpoint_auth;"
        "print('boto3' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"
