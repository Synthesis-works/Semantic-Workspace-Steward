"""Opt-in authentication gate for the deployed SWS MCP endpoint (M17).

REUSE DECISION (see docs/reuse-decisions.md): no SMS reuse. The gate is
fresh, stdlib-only code. The fail-fast startup idea it follows (a configured
but unusable subsystem aborts startup instead of degrading silently) is the
same pattern already used by ``sws_agent.config``.

MCP is transport/exposure only, and so is this module: it authenticates the
*caller of the transport* and nothing else. It makes no authorization
decision, reads no workspace state, and cannot reach the deterministic core.
The single authorization boundary in SWS remains the core plus its tool
allowlist (``agentcore_harness.allowed_tools_patterns``); this gate only
answers "is this HTTP request from the caller we configured?".

Why an application-layer gate:

  - AgentCore Harness authenticates to a remote MCP server with HTTP headers
    only (``remoteMcp.headers``); the value is a credential-provider
    reference that is resolved to a real API key at invocation time. There is
    no mTLS or SigV4 option for a remote MCP server, and an ALB header rule
    would duplicate the secret into rule configuration (and cannot compare in
    constant time). So the check belongs in the ASGI stack.
  - The gate is the *last* line, not the only one: the deployed endpoint is
    private (a VPC-internal NLB whose listener accepts traffic only from the
    harness security group) and is reached over TLS.

Opt-in and fail-closed:

  - With ``SWS_MCP_API_KEY_SECRET_ARN`` absent or blank the endpoint requires
    no credential and this module returns the app unchanged, so local
    development, the hermetic test suite, and every existing deployment path
    behave exactly as before.
  - With it set, the API key is read from AWS Secrets Manager at application
    build time. Every failure mode -- secret unreadable, not JSON, key
    missing, value empty, header name invalid -- raises and aborts startup.
    There is deliberately no fallback: an endpoint that was asked to
    authenticate and silently stopped doing so would be worse than one that
    refuses to start.

Honesty constraints:

  - boto3 is imported lazily inside the reader, exactly like
    ``sws_agent.aws``; importing this module never imports boto3 and never
    performs an AWS call.
  - The API key is never logged, traced, echoed, or included in an error
    message. Comparison uses ``hmac.compare_digest``.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

SWS_MCP_API_KEY_SECRET_ARN_ENV: str = "SWS_MCP_API_KEY_SECRET_ARN"
"""Secrets Manager ARN whose JSON ``api_key`` field the endpoint requires.

Absent or blank opts the endpoint out of authentication entirely. This is the
same single secret the AgentCore Identity credential provider references, so
there is one source of truth for the deployed credential.
"""

SWS_MCP_AUTH_HEADER_ENV: str = "SWS_MCP_AUTH_HEADER"
"""Optional override for the header name carrying the API key."""

DEFAULT_AUTH_HEADER: str = "x-api-key"
"""The header the harness sends (documented AgentCore credential form)."""

API_KEY_JSON_KEY: str = "api_key"
"""JSON field inside the secret holding the key.

A JSON object (rather than a raw string) is required because the AgentCore
credential provider's ``--api-key-secret-config`` takes a ``jsonKey``: the
same secret must satisfy both sides.
"""

TOKEN_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
"""RFC 7230 token: the only legal HTTP header *name* shape."""

_SECRET_ARN_PATTERN = re.compile(
    r"^arn:aws[a-z0-9-]*:secretsmanager:([a-z0-9-]+):([0-9]{12}):secret:(.+)$"
)
"""Secrets Manager ARN shape, used only to recover the region."""


def region_from_secret_arn(secret_arn: str) -> str | None:
    """Return the region named by ``secret_arn``, or ``None``.

    The region is recovered so the reader builds a client bound to the same
    region the secret lives in, instead of depending on an ambient default.
    A non-ARN value returns ``None`` and lets boto3 resolve its own region.
    """
    match = _SECRET_ARN_PATTERN.match(secret_arn)
    if match is None:
        return None
    return match.group(1)


def api_key_from_secret_string(raw: str) -> str:
    """Extract the API key from the secret's JSON body.

    Requires a JSON object whose ``api_key`` field is a non-blank string.
    Surrounding whitespace is stripped on both the stored value and the
    presented header, because a header value cannot carry a newline: a
    trailing newline pasted into the console must not lock the harness out.
    Raises ``ValueError`` without ever echoing the secret contents.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "the endpoint credential secret is not valid JSON; expected an "
            f"object with a non-blank {API_KEY_JSON_KEY!r} string field"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            "the endpoint credential secret must be a JSON object with a "
            f"non-blank {API_KEY_JSON_KEY!r} string field"
        )
    value = parsed.get(API_KEY_JSON_KEY)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "the endpoint credential secret has no non-blank "
            f"{API_KEY_JSON_KEY!r} string field"
        )
    return value.strip()


def read_api_key_from_secrets_manager(secret_arn: str) -> str:
    """Read the API key from Secrets Manager (lazy boto3, no logging).

    Fails closed with ``RuntimeError`` on any AWS error. The underlying
    ``ClientError`` message describes the failure (name, region, permission)
    and never contains the secret value.
    """
    import boto3  # lazy: boto3 stays optional
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client = boto3.client(
            "secretsmanager", region_name=region_from_secret_arn(secret_arn)
        )
        response = client.get_secret_value(SecretId=secret_arn)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(
            f"could not read the endpoint credential secret "
            f"{secret_arn!r}: {type(exc).__name__}: {exc}"
        ) from exc
    raw = response.get("SecretString")
    if not raw:
        raise RuntimeError(
            f"the endpoint credential secret {secret_arn!r} has no "
            "SecretString; binary secrets are not supported"
        )
    return api_key_from_secret_string(raw)


@dataclass(frozen=True)
class EndpointCredential:
    """The resolved caller credential the endpoint requires.

    ``api_key`` is excluded from ``repr`` so an accidental log or traceback
    cannot disclose it.
    """

    header_name: str
    api_key: str = field(repr=False)


def credential_from_environment(
    *,
    environ: Mapping[str, str] | None = None,
    api_key_reader: Callable[[str], str] | None = None,
) -> EndpointCredential | None:
    """Resolve the required credential, or ``None`` when unconfigured.

    ``api_key_reader`` is injectable so tests stay hermetic (no boto3, no
    network, no credentials). It defaults to Secrets Manager.
    """
    env = os.environ if environ is None else environ
    secret_arn = (env.get(SWS_MCP_API_KEY_SECRET_ARN_ENV) or "").strip()
    if not secret_arn:
        return None
    header_name = (
        env.get(SWS_MCP_AUTH_HEADER_ENV) or ""
    ).strip() or DEFAULT_AUTH_HEADER
    if TOKEN_PATTERN.match(header_name) is None:
        raise ValueError(
            f"{SWS_MCP_AUTH_HEADER_ENV} is not a valid HTTP header name: "
            f"{header_name!r}"
        )
    reader = api_key_reader or read_api_key_from_secrets_manager
    api_key = reader(secret_arn)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError(
            f"the endpoint credential secret {secret_arn!r} resolved to no "
            "usable API key"
        )
    return EndpointCredential(
        header_name=header_name.lower(), api_key=api_key.strip()
    )


def gate(
    app: Any,
    *,
    environ: Mapping[str, str] | None = None,
    api_key_reader: Callable[[str], str] | None = None,
) -> Any:
    """Wrap ``app`` with the credential check when one is configured.

    Returns ``app`` unchanged when the endpoint is unconfigured, so the
    opt-in path is a true no-op. Raises (aborting startup) when the endpoint
    is configured but unusable.
    """
    credential = credential_from_environment(
        environ=environ, api_key_reader=api_key_reader
    )
    if credential is None:
        return app
    return require_credential(app, credential=credential)


def _presented_header(headers: Any, name: bytes) -> list[bytes]:
    """All values presented for ``name`` (case-insensitive, bytes ASGI form)."""
    presented: list[bytes] = []
    for raw_name, raw_value in headers or ():
        if bytes(raw_name).lower() == name:
            presented.append(bytes(raw_value))
    return presented


async def _reject(send: Callable[[dict[str, Any]], Any]) -> None:
    """Answer 401 with a fixed body that reflects nothing from the request."""
    body = b'{"error":"unauthorized"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def require_credential(app: Any, *, credential: EndpointCredential) -> Any:
    """Return an ASGI wrapper that requires ``credential`` on every request.

    Non-HTTP scopes (``lifespan``) pass through untouched so the SDK session
    manager still starts. Comparison is constant time and the expected value
    is never echoed.
    """
    expected = credential.api_key.encode("utf-8")
    name = credential.header_name.encode("ascii")

    async def gated(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        for presented in _presented_header(scope.get("headers"), name):
            if hmac.compare_digest(presented.strip(), expected):
                await app(scope, receive, send)
                return
        await _reject(send)

    return gated
