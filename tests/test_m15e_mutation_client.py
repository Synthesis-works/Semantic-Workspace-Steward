"""M15-E: the production mutation client factory and the scoped IAM artifact.

The chain under test is the one ADR 0009 requires to be inspectable without
contacting AWS:

    factory configuration
        -> explicit credentials, explicit region, retry mode
        -> effective boto client configuration
        -> StopInstancesHandler accepts the client

and its inverse:

    wrong / ambient / retry-enabled configuration
        -> handler rejects the client

Nothing here calls ``stop_instances`` against AWS, attaches a policy, or sets
``STOP_RESOURCE.implemented``. Real botocore config objects are constructed
(client creation performs no AWS call); the credential chain is pinned to dummy
values and IMDS is disabled so no test can reach the network.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from typing import Any

import pytest

from sws_agent.ec2_mutation import MutationClientConfigurationError, StopInstancesHandler
from sws_agent.ec2_mutation_client import (
    IAM_POLICY_VERSION,
    MutationClientFactory,
    MutationClientSettings,
    MutationCredentials,
    build_botocore_config,
    effective_retries,
    mutation_iam_policy,
    validate_iam_policy,
)

botocore = pytest.importorskip("botocore", reason="M15-E tests need the AWS SDK")

INSTANCE_ARN = "arn:aws:ec2:us-east-1:123456789012:instance/i-0abcdef1234567890"


def _settings(**overrides: Any) -> MutationClientSettings:
    base: dict[str, Any] = {
        "region": "us-east-1",
        "credentials": MutationCredentials(
            access_key_id="AKIAIOSFODNN7EXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            source="m15e-test",
        ),
        "target_instance_arn": INSTANCE_ARN,
    }
    base.update(overrides)
    return MutationClientSettings(**base)


class _FakeSession:
    """Records what the factory asked for. Returns a client with a real Config."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def client(self, service_name: str, **kwargs: Any) -> Any:
        self.calls.append({"service_name": service_name, **kwargs})
        config = kwargs.get("config")
        return _FakeClient(service_name, config)


class _FakeClient:
    """Shape-compatible with the attributes the factory and handler read."""

    def __init__(self, service_name: str, config: Any) -> None:
        self.meta = _FakeMeta(service_name, config)
        self.stopped: list[dict[str, Any]] = []

    def stop_instances(self, **kwargs: Any) -> dict[str, Any]:
        # Only ever reached by an explicit test that asks for a dispatch. The
        # factory tests below never do.
        self.stopped.append(kwargs)
        return {"StoppingInstances": [{"InstanceId": "i-0abcdef1234567890"}]}


class _FakeMeta:
    def __init__(self, service_name: str, config: Any) -> None:
        self.config = config
        self.service_model = _FakeServiceModel(service_name)


class _FakeServiceModel:
    def __init__(self, service_name: str) -> None:
        self.service_id = service_name


# ---------------------------------------------------------------------------
# 1. Explicit credentials and region. No ambient fallback.
# ---------------------------------------------------------------------------


def test_credentials_are_passed_explicitly_so_the_ambient_chain_is_unreachable():
    session = _FakeSession()
    MutationClientFactory(_settings(), session_factory=lambda: session)()

    call = session.calls[0]
    assert call["service_name"] == "ec2"
    assert call["aws_access_key_id"] == "AKIAIOSFODNN7EXAMPLE"
    assert call["aws_secret_access_key"].startswith("wJalr")
    assert call["region_name"] == "us-east-1"


def test_the_factory_offers_no_way_to_pass_a_profile():
    """The read-only collector's profile must not be reachable from here.

    Not a check that a supplied profile is rejected -- there is no parameter to
    supply one with, so the fallback cannot be reintroduced by a caller.
    """
    import inspect

    parameters = inspect.signature(MutationClientFactory.__init__).parameters
    assert "profile" not in parameters
    assert "profile_name" not in parameters
    assert set(parameters) == {"self", "settings", "session_factory"}

    settings_fields = set(MutationClientSettings.__dataclass_fields__)
    assert "profile" not in settings_fields


def test_a_session_token_is_forwarded_because_a_role_session_needs_one():
    session = _FakeSession()
    creds = MutationCredentials(
        access_key_id="AKIA",
        secret_access_key="secret",
        session_token="FwoGZXIvYXdzE",
        source="assume-role",
    )
    MutationClientFactory(
        _settings(credentials=creds), session_factory=lambda: session
    )()
    assert session.calls[0]["aws_session_token"] == "FwoGZXIvYXdzE"


@pytest.mark.parametrize("missing", ["access_key_id", "secret_access_key", "source"])
def test_blank_credentials_are_refused_before_a_client_can_exist(missing: str):
    creds = MutationCredentials(
        access_key_id="AKIA", secret_access_key="secret", source="x"
    )
    broken_fields = {**creds.__dict__, missing: "   "}
    with pytest.raises(MutationClientConfigurationError):
        MutationCredentials(**broken_fields)
    with pytest.raises(MutationClientConfigurationError):
        _settings(credentials=MutationCredentials(**broken_fields))


@pytest.mark.parametrize("region", ["", "   ", None])
def test_a_blank_region_is_refused(region: Any):
    with pytest.raises(MutationClientConfigurationError):
        _settings(region=region)


def test_no_factory_client_is_built_from_settings_without_credentials():
    with pytest.raises(MutationClientConfigurationError):
        _settings(credentials=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. Retries disabled -- and verified on the real client, not assumed.
# ---------------------------------------------------------------------------


def test_the_configured_retry_value_is_explicit_and_not_a_default():
    config = build_botocore_config(_settings())
    assert config.retries["total_max_attempts"] == 1
    assert config.retries["mode"] == "standard"


def test_a_real_botocore_client_normalizes_the_factory_config_to_one_attempt():
    """The proof the M15-D ruling asked for, against botocore itself.

    ``total_max_attempts`` is chosen over the legacy ``max_attempts`` because
    botocore converts the legacy key to ``max_attempts + 1``. Verified here on a
    real client rather than trusted, so a botocore change to that normalization
    fails this test instead of silently changing the safety property.
    """
    import botocore.session

    session = botocore.session.get_session()
    client = session.create_client(
        "ec2",
        region_name="us-east-1",
        config=build_botocore_config(_settings()),
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    )
    assert effective_retries(client)["total_max_attempts"] == 1
    # The handler must accept the very client the factory builds.
    StopInstancesHandler(client, region="us-east-1")


@pytest.mark.parametrize(
    ("retries", "why"),
    [
        ({"max_attempts": 1}, "one retry: botocore adds the initial request"),
        ({"max_attempts": 3}, "three retries"),
        ({"total_max_attempts": 2}, "one retry"),
        ({"total_max_attempts": 4}, "three retries"),
    ],
)
def test_the_handler_rejects_a_factory_client_that_was_reconfigured_to_retry(
    retries: dict[str, int], why: str
):
    """Defense in depth: the handler does not trust the factory.

    A client this factory produced is accepted when unconfigured, so if anything
    later reconfigures it, only the handler's own precondition stands between a
    second dispatch and the wire. That check is exercised here directly.
    """
    session = _FakeSession()
    client = MutationClientFactory(_settings(), session_factory=lambda: session)()
    # Accepted as built.
    StopInstancesHandler(client, region="us-east-1")

    # Reconfigured afterwards, exactly the case the handler must police.
    config = session.calls[0]["config"]
    object.__setattr__(config, "retries", retries)

    with pytest.raises(MutationClientConfigurationError) as excinfo:
        StopInstancesHandler(client, region="us-east-1")
    assert "dispatch exactly once" in str(excinfo.value), why


def test_an_absent_retry_setting_on_a_built_client_is_refused():
    """botocore substitutes DEFAULT_MAX_ATTEMPTS when unset, so unset != safe."""
    session = _FakeSession()
    client = MutationClientFactory(_settings(), session_factory=lambda: session)()
    config = session.calls[0]["config"]
    object.__setattr__(config, "retries", {})

    with pytest.raises(MutationClientConfigurationError):
        StopInstancesHandler(client, region="us-east-1")


def test_the_factory_refuses_to_return_a_client_that_resolves_toretries():
    """The factory's own read-back check, exercised via a lying config."""

    class _LyingConfig:
        # Looks like the factory's own config to the eye, but resolves to a
        # retrying client. The factory must refuse rather than hand it over.
        retries = {"total_max_attempts": 3, "mode": "standard"}

    session = _FakeSession()
    original = session.client

    def _lying_client(service_name: str, **kwargs: Any) -> Any:
        kwargs["config"] = _LyingConfig()
        return original(service_name, **kwargs)

    session.client = _lying_client  # type: ignore[method-assign]
    with pytest.raises(MutationClientConfigurationError) as excinfo:
        MutationClientFactory(_settings(), session_factory=lambda: session)()
    assert "total_max_attempts" in str(excinfo.value)


def test_the_factory_refuses_a_client_that_is_not_ec2():
    session = _FakeSession()
    original = session.client

    def _wrong_service(service_name: str, **kwargs: Any) -> Any:
        return original("s3", **kwargs)

    session.client = _wrong_service  # type: ignore[method-assign]
    with pytest.raises(MutationClientConfigurationError) as excinfo:
        MutationClientFactory(_settings(), session_factory=lambda: session)()
    assert "ec2" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. The full chain, end to end.
# ---------------------------------------------------------------------------


def test_factory_configuration_reaches_the_handler_as_an_accepted_client():
    """The chain ADR 0009 requires, in one test, with no AWS contact.

        factory configuration
            -> explicit credentials, explicit region, retry mode
            -> effective boto client configuration
            -> StopInstancesHandler accepts the client
    """
    session = _FakeSession()
    factory = MutationClientFactory(_settings(), session_factory=lambda: session)
    client = factory()

    # Stage 1: the factory asked for exactly what it promised.
    call = session.calls[0]
    assert call["service_name"] == "ec2"
    assert call["region_name"] == "us-east-1"
    assert call["config"].retries["total_max_attempts"] == 1

    # Stage 2: the client's effective configuration confirms it.
    assert effective_retries(client)["total_max_attempts"] == 1

    # Stage 3: the handler accepts it, independently, and is wired to it.
    handler = StopInstancesHandler(client, region="us-east-1")
    assert handler.dispatch_calls == ()


def test_everything_ambient_about_the_client_is_refused():
    """The inverse chain, collected in one place.

    wrong / ambient / retry-enabled configuration -> handler rejects the client
    """
    for retries in (
        {"total_max_attempts": 2},
        {"max_attempts": 1},
        {},
        None,
        "standard",
    ):
        session = _FakeSession()
        client = MutationClientFactory(_settings(), session_factory=lambda: session)()
        object.__setattr__(session.calls[0]["config"], "retries", retries)
        with pytest.raises(MutationClientConfigurationError):
            StopInstancesHandler(client, region="us-east-1")


def test_the_settings_instance_id_matches_the_arn_the_handler_would_target():
    settings = _settings()
    assert settings.instance_id == "i-0abcdef1234567890"
    # A mismatch here would mean the handler and the policy disagree about the
    # target, which is precisely the drift the shared settings object prevents.
    assert settings.target_instance_arn.endswith(settings.instance_id)


# ---------------------------------------------------------------------------
# 4. Blast radius: the ARN is constrained on both sides, independently.
# ---------------------------------------------------------------------------


def test_the_single_target_arn_is_accepted():
    assert _settings(target_instance_arn=INSTANCE_ARN).target_instance_arn == INSTANCE_ARN


@pytest.mark.parametrize(
    "arn",
    [
        "*",
        "arn:aws:ec2:us-east-1:123456789012:instance/*",
        "arn:aws:ec2:us-east-1:123456789012:instance/i-abc",
        "arn:aws:ec2:us-east-1:123456789012:volume/vol-0123",
        "arn:aws:s3:::some-bucket",
        "arn:aws:ec2:us-east-1:123456789012:image/ami-0123",
        "arn:aws:ec2:us-east-1:not-an-account:instance/i-0abcdef1234567890",
        "",
        "   ",
        None,
    ],
)
def test_an_arn_that_is_not_one_instance_is_refused(arn: Any):
    """A permissive ARN would be a policy bug, so it is rejected at the source."""
    with pytest.raises(MutationClientConfigurationError):
        _settings(target_instance_arn=arn)


# ---------------------------------------------------------------------------
# 5. The IAM artifact.
# ---------------------------------------------------------------------------


def test_the_generated_policy_grants_stop_on_one_instance_and_describe_on_star():
    policy = mutation_iam_policy(_settings())
    validate_iam_policy(policy, _settings())

    stop, describe = policy["Statement"]
    assert stop["Action"] == ["ec2:StopInstances"]
    assert stop["Resource"] == [INSTANCE_ARN]
    assert describe["Action"] == ["ec2:DescribeInstances"]
    assert describe["Resource"] == "*"
    assert policy["Version"] == IAM_POLICY_VERSION


def test_the_describe_wildcard_is_confined_to_the_one_unavoidable_statement():
    """A '*' resource anywhere but DescribeInstances is a policy bug."""
    settings = _settings()
    policy = mutation_iam_policy(settings)
    wildcards = [
        statement
        for statement in policy["Statement"]
        if "*" in (statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]])
    ]
    assert len(wildcards) == 1
    assert wildcards[0]["Action"] == ["ec2:DescribeInstances"]


def _policy_with(statement_patch: dict[str, Any]) -> dict[str, Any]:
    policy = copy.deepcopy(mutation_iam_policy(_settings()))
    policy["Statement"][0].update(statement_patch)
    return policy


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"Resource": "*"}, "exactly"),
        ({"Resource": ["*"]}, "exactly"),
        ({"Resource": ["arn:aws:ec2:us-east-1:123456789012:instance/*"]}, "exactly"),
        (
            {"Action": ["ec2:StopInstances", "ec2:TerminateInstances"]},
            "outside the mutation identity",
        ),
        ({"Action": ["ec2:*"]}, "wildcard action"),
        ({"Action": ["*"]}, "wildcard action"),
        ({"Action": ["iam:PassRole"]}, "outside the mutation identity"),
        ({"Action": ["ec2:RebootInstances"]}, "outside the mutation identity"),
    ],
)
def test_a_widened_policy_is_refused(patch: dict[str, Any], expected: str):
    """validate_iam_policy re-checks the dict, so a hand-edited policy cannot ship."""
    with pytest.raises(MutationClientConfigurationError) as excinfo:
        validate_iam_policy(_policy_with(patch), _settings())
    assert expected in str(excinfo.value)


def test_a_policy_missing_a_statement_is_refused():
    settings = _settings()
    without_describe = {
        "Version": IAM_POLICY_VERSION,
        "Statement": [mutation_iam_policy(settings)["Statement"][0]],
    }
    with pytest.raises(MutationClientConfigurationError) as excinfo:
        validate_iam_policy(without_describe, settings)
    assert "DescribeInstances" in str(excinfo.value)


def test_a_policy_scoped_to_a_different_instance_is_refused():
    """The policy must match the ARN the handler targets, not merely any ARN."""
    settings = _settings()
    policy = mutation_iam_policy(settings)
    policy["Statement"][0]["Resource"] = [
        "arn:aws:ec2:us-east-1:123456789012:instance/i-11111111111111111"
    ]
    with pytest.raises(MutationClientConfigurationError):
        validate_iam_policy(policy, settings)


def test_a_deny_statement_is_refused_as_out_of_scope():
    settings = _settings()
    policy = mutation_iam_policy(settings)
    policy["Statement"].append(
        {
            "Sid": "DenyEverythingElse",
            "Effect": "Deny",
            "Action": ["ec2:*"],
            "Resource": "*",
        }
    )
    with pytest.raises(MutationClientConfigurationError):
        validate_iam_policy(policy, settings)


# ---------------------------------------------------------------------------
# 6. The boundary itself.
# ---------------------------------------------------------------------------


def test_the_core_modules_stay_sdk_free_and_do_not_import_the_factory():
    """execution.py and ec2_mutation.py must not reach the SDK or this factory."""
    import subprocess

    probe = (
        "import sys; import sws_agent.execution, sws_agent.ec2_mutation; "
        "print(sorted(m for m in sys.modules "
        "if m.split('.')[0] in ('boto3','botocore') "
        "or m == 'sws_agent.ec2_mutation_client'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout


_SDK_BLOCKING_PROBE = """
import importlib.abc
import sys


class _BlockSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("boto3", "botocore"):
            raise ImportError("blocked: " + name)
        return None


sys.meta_path.insert(0, _BlockSDK())
import sws_agent.execution
import sws_agent.ec2_mutation
print("ok")
"""


def test_the_sdk_is_an_optional_extra_and_the_core_imports_without_it():
    """The core must not need boto3; that is why the factory has its own module.

    Asserted by importing the core with the SDK made unimportable, so a future
    change that makes the SDK a hard runtime dependency fails here instead of
    being noticed in an environment that happens to have it installed.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SDK_BLOCKING_PROBE], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_no_composition_wires_the_mutation_factory():
    """The factory existing must not mean the action is reachable.

    The M15-E deliverable is a client and a policy artifact. Neither is wired
    anywhere, and the registry still declares nothing implemented.
    """
    from sws_agent.execution import ACTION_EXECUTION_REGISTRY
    from sws_agent.mcp.server import BUILTIN_TOOL_NAMES, SwsMcpServer

    for action, spec in ACTION_EXECUTION_REGISTRY.items():
        assert spec.implemented is False, action

    server = SwsMcpServer()
    assert server.tool_names() == sorted(BUILTIN_TOOL_NAMES)
    assert "execute_action" not in set(BUILTIN_TOOL_NAMES)

    # Nothing outside the factory's own module may *acquire a client*. The
    # precise property is importing `MutationClientFactory`, not importing the
    # module: M15-F's preflight legitimately reads `effective_retries` and
    # `validate_iam_policy` from here, and those are pure functions that cannot
    # build anything. The distinction was found by this check firing on that
    # legitimate import -- a gate that cannot tell acquisition from inspection
    # gets deleted the first time it is inconvenient, and then it protects
    # nothing. Checked statically so it holds without the SDK extras installed.
    from pathlib import Path

    import sws_agent

    package_root = Path(sws_agent.__file__).parent
    offenders = [
        path.relative_to(package_root).as_posix()
        for path in sorted(package_root.rglob("*.py"))
        if path.name != "ec2_mutation_client.py"
        and _acquires_mutation_client(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"modules able to acquire a mutation client: {offenders}"


#: Forms by which a module could obtain a *client* from this module.
#:
#: Parsed with ``ast`` rather than matched with a regex. Regex was tried first
#: and is wrong in both directions: a bare substring cannot tell a docstring
#: mention from an import, and a line-anchored pattern misses a parenthesized
#: multi-line import -- which a real module may well use for several names. The
#: multi-line miss was found by this check failing on exactly that shape, which
#: is the failure mode a security regex always has: it passes until it doesn't.
_FACTORY_MODULE = "ec2_mutation_client"
_FACTORY_CLASS = "MutationClientFactory"


def _acquires_mutation_client(source: str) -> bool:
    """True when ``source`` can reach a client-building symbol in this module.

    ``ast`` is used so the answer is about imports rather than about text. A
    file that will not parse is reported as acquiring nothing *and* is checked
    separately, so a syntax error cannot quietly disable the gate.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").lstrip(".").split(".")[-1]
            if module != _FACTORY_MODULE:
                continue
            for alias in node.names:
                if alias.name in (_FACTORY_CLASS, "*"):
                    return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == _FACTORY_MODULE:
                    # A bare module import can still reach the class as an
                    # attribute, so it counts as acquisition.
                    return True
    return False


def test_the_acquisition_detector_still_catches_every_real_form():
    """The gate above is only as strong as this function.

    Loosening a security check is only safe if the loosened check is proven to
    still fail on the thing it exists to catch. Each case below is a shape a
    module could plausibly use to obtain a mutation-capable client.
    """
    caught = [
        "from sws_agent.ec2_mutation_client import MutationClientFactory\n",
        "from sws_agent.ec2_mutation_client import (\n    MutationClientFactory,\n)\n",
        "from sws_agent.ec2_mutation_client import (\n    MutationClientSettings,\n"
        "    MutationClientFactory,\n)\n",
        "from .ec2_mutation_client import MutationClientFactory\n",
        "import sws_agent.ec2_mutation_client\n",
        "import sws_agent.ec2_mutation_client as mc\n",
        "import ec2_mutation_client\n",
        "from sws_agent.ec2_mutation_client import *\n",
        "if True:\n    from sws_agent.ec2_mutation_client import MutationClientFactory\n",
        "if True:\n    import sws_agent.ec2_mutation_client\n",
        "async def f():\n    import sws_agent.ec2_mutation_client\n",
    ]
    for source in caught:
        assert _acquires_mutation_client(source), f"missed a real acquisition: {source!r}"


def test_the_acquisition_detector_allows_inspection_without_acquisition():
    """The distinction this detector exists to draw, stated as a test.

    M15-F's preflight imports pure helpers from the factory module. That is
    inspection, not acquisition, and must remain allowed -- otherwise the next
    legitimate need pushes someone to widen the gate instead of fixing it.
    """
    allowed = [
        "from sws_agent.ec2_mutation_client import (\n"
        "    MutationClientSettings,\n    effective_retries,\n    validate_iam_policy,\n)\n",
        "from sws_agent.ec2_mutation_client import mutation_iam_policy\n",
    ]
    for source in allowed:
        assert not _acquires_mutation_client(source), f"false positive: {source!r}"


def test_the_acquisition_detector_ignores_mentions_that_are_not_imports():
    """And the converse: prose must not trip a check that gates on imports."""
    ignored = [
        "See :mod:`sws_agent.ec2_mutation_client` for details.\n",
        "# the ec2_mutation_client module owns the credentials\n",
        '"""Docstring mentioning ec2_mutation_client only."""\n',
        "value = 'ec2_mutation_client'\n",
        "import sws_agent.ec2_mutation  # not the factory module\n",
        "from sws_agent import ec2_mutation_client_helper\n",
        "MutationClientFactory = None  # a local name, not an import\n",
    ]
    for source in ignored:
        assert not _acquires_mutation_client(source), f"false positive: {source!r}"


def test_an_unparseable_module_is_not_treated_as_safe() -> None:
    """A syntax error must not silently read as "acquires nothing".

    The detector returns False on SyntaxError so one bad file cannot crash the
    sweep. That is safe only because every module in the package is separately
    required to import cleanly, which this test pins.
    """
    import ast
    import importlib
    import pkgutil

    import sws_agent

    failures: list[str] = []
    for info in pkgutil.walk_packages(sws_agent.__path__, "sws_agent."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(f"{info.name}: {exc}")
    assert failures == [], f"modules that fail to import: {failures}"
    assert ast.parse("x = 1") is not None


def test_nothing_in_the_factory_module_calls_stop_instances():
    """A direct read of the source: the factory builds clients, never uses them."""
    from pathlib import Path

    import sws_agent.ec2_mutation_client as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # skip the module docstring
    assert "stop_instances(" not in body
    assert ".stop_instances" not in body
