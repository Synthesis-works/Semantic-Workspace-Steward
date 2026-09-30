"""M6 real AWS client factory: hermetic tests.

``sws_agent.aws`` adapts boto3 clients to the existing collector protocols.
Every test here runs credential-free: boto3/botocore are only ever imported
lazily, and all client/session interactions use injected fakes. The only
optional AWS-library check is the ``importorskip("botocore")`` assertion of
the real ``botocore.config.Config`` construction (no network call).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import date, datetime, timezone
import subprocess
import sys

import pytest

from sws_agent.aws import (
    COST_EXPLORER_REGION,
    AwsClientFactory,
    AwsMultiClient,
    SWS_AWS_PROFILE_ENV,
    SWS_AWS_REGION_ENV,
    aws_config_from_env,
)
from sws_agent.config import AWSConnectionConfig
from sws_agent.constants import (
    AWS_API_RETRY_ATTEMPTS,
    AWS_API_TIMEOUT_SECONDS,
    SWSResourceType,
)
from sws_agent.mcp.server import DefaultSwsBackend, SwsMcpServer
from sws_agent.models import WorkspaceSnapshot


def _fn_payload(name: str) -> dict:
    return {
        "FunctionArn": f"arn:aws:lambda:us-east-1:123456789012:function:{name}",
        "FunctionName": name,
        "Runtime": "python3.12",
        "Handler": "app.handler",
        "MemorySize": 128,
        "Timeout": 3,
        "PackageType": "Zip",
        "LastModified": "2026-01-01T00:00:00+00:00",
    }


def _bucket_payload(name: str, *, owner: str | None = "eng") -> dict:
    tags = [{"Key": "Owner", "Value": owner}] if owner is not None else []
    return {
        "Name": name,
        "CreationDate": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        "TagSet": tags,
    }


class FakeClient:
    """Fake boto3-shaped client with canned, per-method responses."""

    def __init__(self, service_name: str, region_name: str | None, config) -> None:
        self.service_name = service_name
        self.region_name = region_name
        self.config = config
        self._responses: dict[str, object] = {}
        self.calls: list[tuple[str, dict]] = []

    def set_response(self, method: str, response: object) -> None:
        self._responses[method] = response

    def __getattr__(self, method: str):
        def handler(**kwargs: object) -> object:
            self.calls.append((method, kwargs))
            if method not in self._responses:
                raise AssertionError(f"unexpected FakeClient call: {method}({kwargs})")
            response = self._responses[method]
            if callable(response):
                return response(**kwargs)
            return response

        return handler


class FakeSession:
    """Fake boto3 Session that records client creation."""

    def __init__(self, profile_name: str | None = None) -> None:
        self.profile_name = profile_name
        self.clients: dict[str, FakeClient] = {}
        self._responses: dict[str, dict[str, object]] = {}

    def set_response(self, service_name: str, method: str, response: object) -> None:
        self._responses.setdefault(service_name, {})[method] = response

    def client(
        self, service_name: str, region_name: str | None = None, config=None
    ) -> FakeClient:
        existing = self.clients.get(service_name)
        if existing is not None:
            return existing
        client = FakeClient(service_name, region_name, config)
        for method, response in self._responses.get(service_name, {}).items():
            client.set_response(method, response)
        self.clients[service_name] = client
        return client


class RecordingConfig:
    """Shaped like the botocore Config fields the factory configures."""

    def __init__(self, retry_attempts: int, timeout_seconds: int) -> None:
        self.retries = {"max_attempts": retry_attempts, "mode": "standard"}
        self.connect_timeout = timeout_seconds
        self.read_timeout = timeout_seconds


def _canned_session_factory(*, buckets, functions, cost_total: str = "12.34"):
    """A session_factory whose clients serve a realistic canned workspace."""

    def session_factory(profile_name: str | None) -> FakeSession:
        session = FakeSession(profile_name=profile_name)
        session.set_response(
            "s3",
            "list_buckets",
            {
                "Buckets": [
                    {"Name": b["Name"], "CreationDate": b["CreationDate"]}
                    for b in buckets
                ],
                "Owner": {"ID": "owner-id", "DisplayName": "owner"},
            },
        )
        session.set_response(
            "s3", "get_bucket_location", {"LocationConstraint": None}
        )
        session.set_response("s3", "get_bucket_tagging", {"TagSet": buckets[0]["TagSet"]})
        session.set_response(
            "lambda",
            "list_functions",
            {"Functions": [_fn_payload(f) for f in functions]},
        )
        session.set_response("lambda", "list_tags", {"Tags": {"Owner": "eng"}})
        session.set_response(
            "ce",
            "get_cost_and_usage",
            {"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": cost_total}}}]},
        )
        return session

    return session_factory


def _factory(
    *,
    region: str = "us-east-1",
    profile: str | None = None,
    session_factory=None,
    client_config_factory=None,
) -> AwsClientFactory:
    return AwsClientFactory(
        AWSConnectionConfig(region=region, profile=profile),
        session_factory=session_factory,
        client_config_factory=client_config_factory or RecordingConfig,
    )


def _payload(result) -> dict:
    content = result.content[0]
    structured = getattr(content, "structured_content", None)
    if structured is not None:
        if isinstance(structured, str):
            return json.loads(structured)
        return structured
    text = getattr(content, "text", None)
    if text is not None:
        return json.loads(text)
    raise AssertionError("tool result carried no JSON payload")


def _run(coro):
    return asyncio.run(coro)


# 1. boto3 is lazy-imported / not required for normal import.
def test_importing_aws_module_never_imports_boto3() -> None:
    code = (
        "import sys;"
        "import sws_agent.aws as aws;"
        "assert 'boto3' not in sys.modules;"
        "assert 'botocore' not in sys.modules;"
        "assert callable(aws.AwsClientFactory);"
        "print('SWS_AWS_IMPORT_OK')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "SWS_AWS_IMPORT_OK" in completed.stdout


def test_factory_construction_never_builds_clients_or_sessions() -> None:
    invoked: list[str | None] = []
    factory = _factory(
        session_factory=lambda profile: (invoked.append(profile), FakeSession(profile))[1]
    )
    assert invoked == []
    multi = factory()
    assert isinstance(multi, AwsMultiClient)
    assert invoked == [None]


# 2. AWSConnectionConfig maps to Session parameters.
def test_region_maps_to_client_regions() -> None:
    sessions: list[FakeSession] = []
    profiles: list[str | None] = []

    def session_factory(profile_name: str | None) -> FakeSession:
        profiles.append(profile_name)
        session = FakeSession(profile_name)
        sessions.append(session)
        return session

    _factory(region="us-west-2", session_factory=session_factory)()
    assert profiles == [None]
    session = sessions[0]
    assert set(session.clients) == {"s3", "lambda", "ce"}
    assert session.clients["s3"].region_name == "us-west-2"
    assert session.clients["lambda"].region_name == "us-west-2"
    assert session.clients["ce"].region_name == COST_EXPLORER_REGION


def test_profile_maps_to_session_profile() -> None:
    profiles: list[str | None] = []

    def session_factory(profile_name: str | None) -> FakeSession:
        profiles.append(profile_name)
        return FakeSession(profile_name)

    _factory(profile="dev", session_factory=session_factory)()
    assert profiles == ["dev"]


def test_region_and_profile_both_map() -> None:
    captured: dict = {}

    def session_factory(profile_name: str | None) -> FakeSession:
        captured["profile"] = profile_name
        session = FakeSession(profile_name)
        captured["session"] = session
        return session

    _factory(region="eu-west-1", profile="work", session_factory=session_factory)()
    assert captured["profile"] == "work"
    assert captured["session"].clients["s3"].region_name == "eu-west-1"
    assert captured["session"].clients["lambda"].region_name == "eu-west-1"


# 3. Retry / timeout configuration uses the canonical constants.
def test_factory_applies_canonical_retry_and_timeout_limits() -> None:
    sent_configs: list[RecordingConfig] = []

    def config_factory(retry_attempts: int, timeout_seconds: int) -> RecordingConfig:
        config = RecordingConfig(retry_attempts, timeout_seconds)
        sent_configs.append(config)
        return config

    factory = _factory(
        session_factory=lambda profile: FakeSession(profile),
        client_config_factory=config_factory,
    )
    factory()
    assert len(sent_configs) == 1
    config = sent_configs[0]
    assert config.retries["max_attempts"] == AWS_API_RETRY_ATTEMPTS
    assert config.retries["mode"] == "standard"
    assert config.connect_timeout == AWS_API_TIMEOUT_SECONDS
    assert config.read_timeout == AWS_API_TIMEOUT_SECONDS


def test_real_botocore_config_uses_canonical_limits() -> None:
    pytest.importorskip("botocore")
    config = AwsClientFactory._botocore_config(
        AWS_API_RETRY_ATTEMPTS, AWS_API_TIMEOUT_SECONDS
    )
    assert config.retries["max_attempts"] == AWS_API_RETRY_ATTEMPTS
    assert config.retries["mode"] == "standard"
    assert config.connect_timeout == AWS_API_TIMEOUT_SECONDS
    assert config.read_timeout == AWS_API_TIMEOUT_SECONDS


# 4. S3 / Lambda / Cost Explorer clients are all created by the factory.
def test_factory_creates_s3_lambda_and_ce_clients() -> None:
    session = FakeSession()
    _factory(session_factory=lambda profile: session)()
    assert set(session.clients) == {"s3", "lambda", "ce"}


def test_factory_builds_fresh_clients_per_call() -> None:
    sessions: list[FakeSession] = []
    factory = _factory(
        session_factory=lambda profile: (sessions.append(None) or FakeSession(profile))
    )
    factory()
    factory()
    assert len(sessions) == 2


# 5. No credentials printed / exposed.
def test_aws_module_has_no_credential_literals() -> None:
    from sws_agent import aws

    source = inspect.getsource(aws)
    for marker in (
        "aws_access_key_id",
        "aws_secret_access_key",
        "secret_access_key",
        "session_token",
        "password",
    ):
        assert marker not in source


def test_factory_holds_no_credential_state() -> None:
    factory = _factory()
    assert factory._config.profile is None
    assert factory._config.region == "us-east-1"


# 6. env resolution: SWS-prefixed, opt-in, blank-tolerant.
def test_env_config_absent_by_default(monkeypatch) -> None:
    monkeypatch.delenv(SWS_AWS_REGION_ENV, raising=False)
    monkeypatch.delenv(SWS_AWS_PROFILE_ENV, raising=False)
    assert aws_config_from_env() is None


def test_env_config_region(monkeypatch) -> None:
    monkeypatch.setenv(SWS_AWS_REGION_ENV, "ap-south-1")
    monkeypatch.delenv(SWS_AWS_PROFILE_ENV, raising=False)
    config = aws_config_from_env()
    assert config is not None
    assert config.region == "ap-south-1"
    assert config.profile is None


def test_env_config_profile(monkeypatch) -> None:
    monkeypatch.delenv(SWS_AWS_REGION_ENV, raising=False)
    monkeypatch.setenv(SWS_AWS_PROFILE_ENV, "worker")
    config = aws_config_from_env()
    assert config is not None
    assert config.profile == "worker"
    assert config.region is None


def test_env_config_blank_values_treated_as_unset(monkeypatch) -> None:
    monkeypatch.setenv(SWS_AWS_REGION_ENV, "   ")
    monkeypatch.delenv(SWS_AWS_PROFILE_ENV, raising=False)
    assert aws_config_from_env() is None


# 7. DefaultSwsBackend can use the factory with fake clients.
def test_backend_collect_workspace_through_factory() -> None:
    backend = DefaultSwsBackend(
        client_factory=_factory(
            session_factory=_canned_session_factory(
                buckets=[_bucket_payload("b-1")], functions=["fn-1"]
            )
        )
    )
    snapshot = backend.collect_workspace(regions=["us-east-1"], limit=10)
    assert isinstance(snapshot, WorkspaceSnapshot)
    assert snapshot.counts == {
        SWSResourceType.S3_BUCKET: 1,
        SWSResourceType.LAMBDA_FUNCTION: 1,
    }
    assert snapshot.partial is False
    assert snapshot.truncated is False
    assert snapshot.failures == []


def test_backend_get_cost_estimates_through_factory() -> None:
    backend = DefaultSwsBackend(
        client_factory=_factory(
            session_factory=_canned_session_factory(
                buckets=[_bucket_payload("b-1")], functions=[]
            )
        )
    )
    report = backend.get_cost_estimates(
        end_date=date(2026, 2, 1), window_days=7
    )
    assert len(report.estimates) == 1
    assert report.estimates[0].line_item == "total"
    assert report.estimates[0].amount_usd == 12.34
    assert report.estimates[0].projected is True
    assert report.truncated is False
    assert report.failures == []


# 8. MCP tools flow through the injected factory end-to-end.
def test_collect_workspace_tool_through_factory() -> None:
    server = SwsMcpServer(
        backend=DefaultSwsBackend(
            client_factory=_factory(
                session_factory=_canned_session_factory(
                    buckets=[_bucket_payload("b-1")], functions=["fn-1"]
                )
            )
        )
    )
    result = _run(
        server.call_tool("collect_workspace", {"regions": ["us-east-1"], "limit": 10})
    )
    assert result.is_error is False
    payload = _payload(result)
    assert payload["snapshot"]["counts"]["s3_bucket"] == 1
    assert payload["snapshot"]["counts"]["lambda_function"] == 1
    assert payload["snapshot"]["partial"] is False


def test_get_cost_estimates_tool_through_factory() -> None:
    server = SwsMcpServer(
        backend=DefaultSwsBackend(
            client_factory=_factory(
                session_factory=_canned_session_factory(
                    buckets=[_bucket_payload("b-1")], functions=[]
                )
            )
        )
    )
    result = _run(
        server.call_tool(
            "get_cost_estimates", {"end_date": "2026-02-01", "window_days": 7}
        )
    )
    assert result.is_error is False
    payload = _payload(result)
    assert payload["cost_estimates"][0]["line_item"] == "total"


def test_audit_workspace_tool_end_to_end() -> None:
    server = SwsMcpServer(
        backend=DefaultSwsBackend(
            client_factory=_factory(
                session_factory=_canned_session_factory(
                    buckets=[_bucket_payload("b-1")], functions=["fn-1"]
                )
            )
        )
    )
    result = _run(
        server.call_tool(
            "audit_workspace",
            {
                "regions": ["us-east-1"],
                "limit": 10,
                "collect_cost": True,
                "cost_end_date": "2026-02-01",
                "cost_window_days": 7,
                "cost_group_by": ["service"],
            },
        )
    )
    assert result.is_error is False
    payload = _payload(result)
    assert payload["snapshot"]["counts"]["s3_bucket"] == 1
    assert payload["snapshot"]["counts"]["lambda_function"] == 1
    assert len(payload["relationships"]) == 2  # same_region + same_owner_tag
    for relationship in payload["relationships"]:
        assert relationship["basis"] == "deterministic"
        assert relationship["claim_kind"] == "derived"
    assert len(payload["decisions"]) == 2
    assert {d["recommended_action"] for d in payload["decisions"]} == {"leave"}
    assert len(payload["snapshot"]["cost"]) == 1


# 9. Client layer never changes deterministic policy output.
def test_deterministic_policy_unchanged_by_client_layer() -> None:
    tagged_bucket = {
        "Name": "b-owned",
        "CreationDate": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        "TagSet": [{"Key": "Owner", "Value": "eng"}],
    }
    untagged_bucket = {
        "Name": "b-orphan",
        "CreationDate": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        "TagSet": [],
    }

    def session_factory(profile_name: str | None) -> FakeSession:
        session = FakeSession(profile_name)
        session.set_response(
            "s3",
            "list_buckets",
            {
                "Buckets": [tagged_bucket, untagged_bucket],
                "Owner": {"ID": "owner-id", "DisplayName": "owner"},
            },
        )
        session.set_response("s3", "get_bucket_location", {"LocationConstraint": None})
        session.set_response(
            "s3",
            "get_bucket_tagging",
            lambda **kw: {
                "TagSet": (
                    [{"Key": "Owner", "Value": "eng"}]
                    if kw.get("Bucket") == "b-owned"
                    else []
                )
            },
        )
        session.set_response("lambda", "list_functions", {"Functions": []})
        session.set_response("lambda", "list_tags", {"Tags": {}})
        session.set_response(
            "ce",
            "get_cost_and_usage",
            {"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": "0"}}}]},
        )
        return session

    backend = DefaultSwsBackend(client_factory=_factory(session_factory=session_factory))
    snapshot = backend.collect_workspace(regions=["us-east-1"], limit=10)
    decisions = {d.resource_id: d for d in backend.evaluate_workspace(snapshot)}
    assert decisions["b-owned"].recommended_action.value == "leave"
    assert decisions["b-owned"].rule is None
    assert decisions["b-orphan"].recommended_action.value == "flag_for_review"
    assert decisions["b-orphan"].rule == "missing_owner_tag"
    assert decisions["b-orphan"].confidence == 1.0


# 10. Existing no-factory deterministic ToolError remains intact.
def test_no_factory_toolerror_remains_deterministic() -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    server = SwsMcpServer()
    with pytest.raises(ToolError) as exc:
        _run(server.call_tool("collect_workspace", {"regions": ["us-east-1"]}))
    assert "no AWS client factory" in str(exc.value)


# ---------------------------------------------------------------------------
# 11. M11: the single read-only EC2 seam. Additive only -- no existing test
#     above is modified, and the ``ec2`` client stays lazy so the inventory
#     tests that assert exactly {s3, lambda, ce} keep holding.
# ---------------------------------------------------------------------------


class FakeEc2Paginator:
    """Fake ``ec2:DescribeInstances`` paginator."""

    def __init__(self, pages: list[dict] | None = None) -> None:
        self._pages = pages or [{"Reservations": []}]
        self.paginate_calls: list[dict] = []

    def paginate(self, **kwargs: object) -> object:
        self.paginate_calls.append(kwargs)
        return iter(self._pages)


class FakeEc2Client:
    """Fake boto3 ``ec2`` client exposing only the paginated read."""

    def __init__(self, region_name: str | None, config) -> None:
        self.service_name = "ec2"
        self.region_name = region_name
        self.config = config
        self.paginator_requests: list[str] = []

    def get_paginator(self, operation_name: str) -> FakeEc2Paginator:
        self.paginator_requests.append(operation_name)
        return FakeEc2Paginator()

    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected EC2 client attribute access: {name}")


def _ec2_session(profile_name: str | None = None) -> FakeSession:
    """A FakeSession that serves a FakeEc2Client for the ``ec2`` service."""
    session = FakeSession(profile_name=profile_name)

    def client(
        service_name: str, region_name: str | None = None, config=None
    ) -> FakeClient:
        if service_name == "ec2":
            existing = session.clients.get("ec2")
            if existing is not None:
                return existing
            built = FakeEc2Client(region_name, config)
            session.clients["ec2"] = built  # type: ignore[assignment]
            return built  # type: ignore[return-value]
        return FakeSession.client(session, service_name, region_name, config)

    session.client = client  # type: ignore[method-assign]
    return session


def test_ec2_client_uses_the_configured_region_and_profile() -> None:
    session = _ec2_session()
    profiles: list[str | None] = []
    factory = AwsClientFactory(
        AWSConnectionConfig(region="us-west-2", profile="dev"),
        session_factory=lambda profile: (profiles.append(profile), session)[1],
        client_config_factory=RecordingConfig,
    )
    multi = factory()
    multi.describe_instances()
    assert profiles == ["dev"]
    assert session.clients["ec2"].region_name == "us-west-2"
    assert session.clients["s3"].region_name == "us-west-2"


def test_ec2_client_inherits_retry_and_timeout_configuration() -> None:
    session = _ec2_session()
    _factory(session_factory=lambda profile: session)().describe_instances()
    config = session.clients["ec2"].config
    assert config.retries["max_attempts"] == AWS_API_RETRY_ATTEMPTS
    assert config.retries["mode"] == "standard"
    assert config.connect_timeout == AWS_API_TIMEOUT_SECONDS
    assert config.read_timeout == AWS_API_TIMEOUT_SECONDS


def test_describe_instances_returns_the_describe_instances_paginator() -> None:
    session = _ec2_session()
    paginator = _factory(session_factory=lambda profile: session)().describe_instances()
    assert isinstance(paginator, FakeEc2Paginator)
    assert session.clients["ec2"].paginator_requests == ["describe_instances"]


def test_multi_client_exposes_exactly_seven_operations() -> None:
    expected = {
        "list_buckets",
        "get_bucket_location",
        "get_bucket_tagging",
        "list_functions",
        "list_tags",
        "get_cost_and_usage",
        "describe_instances",
    }
    public = {
        name
        for name in dir(AwsMultiClient)
        if not name.startswith("_") and callable(getattr(AwsMultiClient, name, None))
    }
    assert public == expected


def test_multi_client_has_no_generic_ec2_passthrough() -> None:
    for name in ("call", "invoke", "request", "ec2_method", "client", "get_client"):
        assert not hasattr(AwsMultiClient, name), name
    signature = inspect.signature(AwsMultiClient.describe_instances)
    assert list(signature.parameters) == ["self"]
    source = inspect.getsource(AwsMultiClient)
    assert "stop_instances" not in source
    assert "getattr(" not in source


def test_existing_six_methods_are_unchanged() -> None:
    s3 = FakeClient("s3", "us-east-1", None)
    s3.set_response("list_buckets", {"Buckets": []})
    s3.set_response("get_bucket_location", {"LocationConstraint": None})
    s3.set_response("get_bucket_tagging", {"TagSet": []})
    lambda_client = FakeClient("lambda", "us-east-1", None)
    lambda_client.set_response("list_functions", {"Functions": []})
    lambda_client.set_response("list_tags", {"Tags": {}})
    ce = FakeClient("ce", COST_EXPLORER_REGION, None)
    ce.set_response("get_cost_and_usage", {"ResultsByTime": []})

    multi = AwsMultiClient(s3=s3, lambda_client=lambda_client, cost_explorer=ce)
    multi.list_buckets()
    multi.get_bucket_location(Bucket="b")
    multi.get_bucket_tagging(Bucket="b")
    multi.list_functions()
    multi.list_tags(Resource="fn")
    multi.get_cost_and_usage(TimePeriod={"Type": "MONTH"})

    assert [method for method, _ in s3.calls] == [
        "list_buckets",
        "get_bucket_location",
        "get_bucket_tagging",
    ]
    assert s3.calls[1][1] == {"Bucket": "b"}
    assert [method for method, _ in lambda_client.calls] == ["list_functions", "list_tags"]
    assert [method for method, _ in ce.calls] == ["get_cost_and_usage"]


def test_describe_instances_without_an_ec2_factory_fails_loudly() -> None:
    multi = AwsMultiClient(
        s3=FakeClient("s3", None, None),
        lambda_client=FakeClient("lambda", None, None),
        cost_explorer=FakeClient("ce", None, None),
    )
    with pytest.raises(RuntimeError, match="no EC2 client factory"):
        multi.describe_instances()


def test_no_ec2_client_is_constructed_until_an_instance_is_observed() -> None:
    session = _ec2_session()
    builds: list[FakeEc2Client] = []
    original_client = session.client

    def counting_client(
        service_name: str, region_name: str | None = None, config=None
    ) -> FakeClient:
        built = original_client(service_name, region_name, config)
        if service_name == "ec2" and session.clients.get("ec2") is built:
            builds.append(built)
        return built

    session.client = counting_client  # type: ignore[method-assign]
    multi = _factory(session_factory=lambda profile: session)()
    # Inventory-only usage never builds an EC2 client.
    assert set(session.clients) == {"s3", "lambda", "ce"}
    assert builds == []

    multi.describe_instances()
    assert set(session.clients) == {"s3", "lambda", "ce", "ec2"}
    assert len(builds) == 1

    multi.describe_instances()
    assert len(builds) == 1  # reused, not rebuilt
    assert session.clients["ec2"].paginator_requests == ["describe_instances"] * 2
