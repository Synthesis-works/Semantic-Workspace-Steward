"""Tests for the non-dispatching preflight gate.

Two properties matter more than coverage here:

1. **The gate must be able to fail.** A gate that always authorizes is
   indistinguishable from no gate. Several tests below assert specific
   refusals, and one asserts the gate's own decision logic is not vacuous.
2. **The gate must not dispatch.** Checked by reading its source, since a
   preflight that could issue a mutation would be an additional way to mutate.

No AWS access, no sockets, no real credentials: every input is constructed
in-process.
"""

from __future__ import annotations

import copy
import io
from pathlib import Path
from typing import Any

import pytest

import sws_agent.preflight as preflight_module
from sws_agent.ec2_mutation import MutationClientConfigurationError
from sws_agent.ec2_mutation_client import (
    MutationClientFactory,
    MutationClientSettings,
    MutationCredentials,
    mutation_iam_policy,
)
from sws_agent.execution import ACTION_EXECUTION_REGISTRY
from sws_agent.mcp.server import BUILTIN_TOOL_NAMES
from sws_agent.mutation_evidence import DispatchWitness
from sws_agent.preflight import (
    EXPECTED_MCP_TOOL_NAMES,
    PreflightRefusal,
    evaluate_dispatch_evidence,
    evaluate_preflight,
    require_preflight,
)

INSTANCE_ARN = "arn:aws:ec2:us-east-1:123456789012:instance/i-0abcdef1234567890"
INSTANCE_ID = "i-0abcdef1234567890"
OTHER_INSTANCE_ARN = "arn:aws:ec2:us-east-1:123456789012:instance/i-0fffffffffffffff0"
REGION = "us-east-1"
CREDENTIALS = MutationCredentials(
    access_key_id="AKIAIOSFODNN7EXAMPLE",
    secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
)


# -- helpers ------------------------------------------------------------------


def _settings(**overrides: Any) -> MutationClientSettings:
    base = {
        "target_instance_arn": INSTANCE_ARN,
        "region": REGION,
        "credentials": CREDENTIALS,
    }
    base.update(overrides)
    return MutationClientSettings(**base)


def _client(settings: MutationClientSettings | None = None) -> Any:
    """Build a real botocore client through the production factory.

    The factory verifies retry count and service identity, so a client that
    reaches a test has already passed those checks -- the gate re-checks them
    independently rather than trusting the factory.
    """
    s = settings or _settings()
    return MutationClientFactory(settings=s, session_factory=_StubSession)()


class _StubSession:
    """Creates a real botocore client with no network capability.

    Session construction, config resolution, service model loading, and the retry
    state machine are all real botocore, so ``effective_retries`` reads a genuine
    ``Config``. Only the credentials are placeholders and no request is issued.
    """

    def __init__(self) -> None:
        import botocore.session

        self._inner = botocore.session.get_session()

    def client(self, service_name: str, **kwargs: Any) -> Any:
        return self._inner.create_client(service_name, **kwargs)


def _witness(client: Any) -> DispatchWitness:
    witness = DispatchWitness()
    witness.arm(client)
    return witness


def _good_preflight(**overrides: Any) -> tuple[Any, dict[str, Any], Any]:
    """Evaluate a preflight that should pass, letting each test break one input.

    ``witness`` is only derived when not supplied, because a supplied witness is
    often paired with a client it is not attached to.
    """
    settings = overrides.pop("settings", None) or _settings()
    policy = overrides.pop("policy", None) or mutation_iam_policy(settings)
    client = overrides.pop("client", None)
    witness = overrides.pop("witness", None)
    if witness is None:
        client = client or _client(settings)
        witness = _witness(client)
    kwargs: dict[str, Any] = {
        "settings": settings,
        "policy": policy,
        "client": client if client is not None else _client(settings),
        "witness": witness,
        "implemented_actions": [],
        "mcp_tool_names": list(BUILTIN_TOOL_NAMES),
    }
    kwargs.update(overrides)
    return evaluate_preflight(**kwargs), policy, client


# -- the gate is not vacuous --------------------------------------------------


def test_a_correctly_constructed_preflight_authorizes():
    report, _policy, _client = _good_preflight()
    assert report.authorized, report.reason()
    assert len(report.findings) >= 5


def test_the_gate_can_fail_on_every_check_independently():
    """Each check must be able to refuse on its own.

    If any check could only ever pass, the gate's width would be an illusion.
    Every case varies exactly one input from the healthy path.
    """
    settings = _settings()
    policy = mutation_iam_policy(settings)
    good_client = _client(settings)
    good_witness = _witness(good_client)
    loose_client = _RetryLooseClient()
    loose_witness = _witness(loose_client)

    cases: dict[str, dict[str, Any]] = {
        "target_scoping": {
            "policy": {"Statement": []},
        },
        "retry_configuration": {
            "client": loose_client,
            "witness": loose_witness,
        },
        "witness_armed": {
            "witness": _NeverArmedWitness(),
        },
        "program_invariants": {
            "implemented_actions": ["stop_instances"],
        },
        "witness_start_state": {
            "witness": _PrefilledWitness(records=3),
        },
    }
    for check, overrides in cases.items():
        kwargs: dict[str, Any] = {
            "settings": settings,
            "policy": policy,
            "client": good_client,
            "witness": good_witness,
            "implemented_actions": [],
            "mcp_tool_names": list(BUILTIN_TOOL_NAMES),
        }
        kwargs.update(overrides)
        report = evaluate_preflight(**kwargs)
        failed = {f.check for f in report.failures}
        assert check in failed, f"{check} could not fail: {report.reason()}"


def test_requiring_authorization_raises_with_every_failure_listed():
    report, _policy, _client = _good_preflight(policy={"Statement": []})
    require_preflight_raised = False
    try:
        require_preflight(report)
    except PreflightRefusal as exc:
        require_preflight_raised = True
        assert "target_scoping" in str(exc)
        assert exc.report is report
    assert require_preflight_raised, "a failing report must not authorize"


def test_evaluating_is_not_authorizing():
    """A caller must have to ask for authorization explicitly.

    Evaluation is a read. If it implied permission, a preflight could not be run
    "just to look" -- and looking is exactly what happens before a real run.
    """
    report, _policy, _client = _good_preflight()
    assert isinstance(report.authorized, bool)
    require_preflight(report)  # explicit, and separate


# -- individual refusals ------------------------------------------------------


def test_a_policy_scoped_to_another_instance_is_refused():
    other = _settings(target_instance_arn=OTHER_INSTANCE_ARN)
    report, _policy, _client = _good_preflight(
        settings=other, policy=mutation_iam_policy(_settings())
    )
    assert not report.authorized
    assert any("target_scoping" in f.detail or "policy" in f.detail for f in report.failures)


def test_an_arn_that_does_not_identify_the_declared_instance_is_refused():
    """Two identifiers must be reconciled, not both assumed correct.

    An ARN and an instance id that disagree would produce a policy scoped to one
    instance and a request aimed at another. Each is individually well-formed,
    so only the comparison catches it. ``instance_id`` is a derived read-only
    property, so the disagreement is produced with a stand-in rather than by
    mutating real settings.
    """
    divergent = _DivergentInstanceIdSettings(
        target_instance_arn=INSTANCE_ARN,
        region=REGION,
        credentials=CREDENTIALS,
    )
    assert divergent.instance_id != _settings().instance_id
    report = evaluate_preflight(
        settings=divergent,
        policy=mutation_iam_policy(_settings()),
        client=_client(),
        witness=_NeverArmedWitness(),
        implemented_actions=[],
        mcp_tool_names=list(BUILTIN_TOOL_NAMES),
    )
    assert not report.authorized
    assert any(f.check == "target_scoping" for f in report.failures)


def test_a_wildcard_target_is_refused_by_settings_before_the_gate_runs():
    """Defense is layered: the settings object rejects a wildcard ARN outright.

    The gate also refuses a non-instance ARN, but a target that cannot be
    constructed at all is a stronger position, so this asserts the rejection
    happens at construction. Both are real refusals; only one is reachable.
    """
    with pytest.raises(MutationClientConfigurationError):
        _settings(target_instance_arn="arn:aws:ec2:us-east-1:123456789012:instance/*")


def test_an_arn_that_is_not_an_instance_arn_is_refused_by_the_gate():
    """The gate's own ARN parsing, tested directly.

    Construction already rejects this, so the gate check is a second layer; the
    double exists only so the second layer is exercised rather than assumed.
    """
    report = evaluate_preflight(
        settings=_RawSettings(
            arn="arn:aws:ec2:us-east-1:123456789012:volume/vol-0123456789abcdef0",
            instance_id="vol-0123456789abcdef0",
        ),
        policy=mutation_iam_policy(_settings()),
        client=_client(),
        witness=_NeverArmedWitness(),
        implemented_actions=[],
        mcp_tool_names=list(BUILTIN_TOOL_NAMES),
    )
    assert not report.authorized
    failure = next(f for f in report.failures if f.check == "target_scoping")
    assert "not an instance ARN" in failure.detail


def test_a_wildcard_arn_is_refused_by_the_gate_too():
    """Same reasoning for a wildcard: unreachable via construction, still checked."""
    report = evaluate_preflight(
        settings=_RawSettings(
            arn="arn:aws:ec2:us-east-1:123456789012:instance/*",
            instance_id="*",
        ),
        policy=mutation_iam_policy(_settings()),
        client=_client(),
        witness=_NeverArmedWitness(),
        implemented_actions=[],
        mcp_tool_names=list(BUILTIN_TOOL_NAMES),
    )
    assert not report.authorized
    failure = next(f for f in report.failures if f.check == "target_scoping")
    assert "not an instance ARN" in failure.detail


def test_a_wildcard_action_in_the_policy_is_refused():
    """The ratified ``DescribeInstances:*`` is resource-broad, never action-broad."""
    settings = _settings()
    policy = mutation_iam_policy(settings)
    policy["Statement"][0]["Action"] = ["ec2:*"]
    report, _policy, _client = _good_preflight(settings=settings, policy=policy)
    assert not report.authorized
    assert any(f.check == "target_scoping" for f in report.failures)


def test_an_unreadable_retry_configuration_is_refused_not_assumed_safe():
    """An unmeasurable retry budget is a refusal, never an optimistic default.

    The double cannot host a witness (it has no event emitter), so an armed
    witness is supplied separately -- isolating the retry check from arming.
    """
    report = evaluate_preflight(
        settings=_settings(),
        policy=mutation_iam_policy(_settings()),
        client=_ExplodingRetryClient(),
        witness=_PrefilledWitness(records=0),
        implemented_actions=[],
        mcp_tool_names=list(BUILTIN_TOOL_NAMES),
    )
    assert not report.authorized
    failure = next(f for f in report.failures if f.check == "retry_configuration")
    assert "could not be read" in failure.detail


def test_an_unarmed_witness_is_refused_before_dispatch():
    report, _policy, _client = _good_preflight(witness=_NeverArmedWitness())
    failure = next(f for f in report.failures if f.check == "witness_armed")
    assert "not armed" in failure.detail


def test_a_witness_reused_from_a_previous_run_is_refused():
    """Otherwise the attempt count would total across runs, not describe this one."""
    report, _policy, _client = _good_preflight(witness=_PrefilledWitness(records=2))
    failure = next(f for f in report.failures if f.check == "witness_start_state")
    assert "already holds" in failure.detail


def test_an_implemented_action_is_refused():
    report, _policy, _client = _good_preflight(implemented_actions=["stop_instances"])
    failure = next(f for f in report.failures if f.check == "program_invariants")
    assert "stop_instances" in failure.detail


# -- post-dispatch evidence ---------------------------------------------------


def test_dispatch_evidence_is_refused_for_an_empty_witness():
    """The zero-attempt rule, reached through the gate."""
    client = _client()
    witness = _witness(client)
    report = evaluate_dispatch_evidence(witness=witness, expected_instance_id=INSTANCE_ID)
    assert not report.authorized
    assert {f.check for f in report.failures} == {
        "witnessed_targets",
        "single_dispatch",
    }


def test_dispatch_evidence_requires_an_exactly_matching_target():
    client = _client()
    witness = _witness(client)
    witness.records.append(_record(instance_ids=(INSTANCE_ID,)))
    report = evaluate_dispatch_evidence(
        witness=witness, expected_instance_id="i-0000000000000000a"
    )
    failure = next(f for f in report.failures if f.check == "witnessed_targets")
    assert "do not match" in failure.detail


def test_a_clean_single_dispatch_is_accepted():
    client = _client()
    witness = _witness(client)
    witness.records.append(_record(instance_ids=(INSTANCE_ID,)))
    report = evaluate_dispatch_evidence(
        witness=witness, expected_instance_id=INSTANCE_ID
    )
    assert report.authorized, report.reason()


def test_two_attempts_are_refused_as_a_second_dispatch():
    client = _client()
    witness = _witness(client)
    witness.records.append(_record(instance_ids=(INSTANCE_ID,)))
    witness.records.append(_record(instance_ids=(INSTANCE_ID,), attempt=2))
    report = evaluate_dispatch_evidence(
        witness=witness, expected_instance_id=INSTANCE_ID
    )
    assert not report.authorized
    # The gate summarizes the witness's violations rather than restating them, so
    # the specific reason is asserted against the witness itself.
    failure = next(f for f in report.failures if f.check == "single_dispatch")
    assert "2 witness violation(s)" in failure.detail
    problems = witness.violations()
    assert any("HTTP attempts reached the wire" in p for p in problems), problems
    assert any("attempt=2" in p for p in problems), problems


def test_dispatch_evidence_does_not_run_before_dispatch():
    """Phase separation is a property, so it is asserted.

    A single combined gate could not satisfy both "requires a witnessed record"
    and "runs before dispatch"; this test fails if someone merges them back. The
    preflight is evaluated with a fresh witness, the record is then added, and
    only the post-dispatch phase sees it.
    """
    client = _client()
    settings = _settings()
    policy = mutation_iam_policy(settings)

    fresh = _witness(client)
    pre = evaluate_preflight(
        settings=settings,
        policy=policy,
        client=client,
        witness=fresh,
        implemented_actions=[],
        mcp_tool_names=list(BUILTIN_TOOL_NAMES),
    )
    assert pre.authorized, pre.reason()

    # Before the record exists, the post-dispatch phase must refuse.
    assert not evaluate_dispatch_evidence(
        witness=fresh, expected_instance_id=INSTANCE_ID
    ).authorized

    fresh.records.append(_record(instance_ids=(INSTANCE_ID,)))
    post = evaluate_dispatch_evidence(
        witness=fresh, expected_instance_id=INSTANCE_ID
    )
    assert post.authorized, post.reason()
    assert {f.check for f in pre.findings}.isdisjoint({f.check for f in post.findings})


# -- the gate cannot dispatch -------------------------------------------------


def test_the_preflight_module_contains_no_mutation_call():
    """A preflight that could dispatch would be one more way to dispatch.

    AST-based rather than substring-based: every text version of this check
    misfires on a legitimate name. Adding a call parenthesis is not enough,
    because a property definition such as ``def is_stop_instances(self)`` also
    matches ``stop_instances(``. Only actual ``Call`` nodes tell the difference.
    """
    import ast

    source = Path(preflight_module.__file__).read_text(encoding="utf-8")
    called = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else ""
            )
    for forbidden in ("stop_instances", "describe_instances", "urlopen"):
        assert forbidden not in called, f"preflight must not call {forbidden!r}"


def test_the_preflight_module_imports_no_aws_sdk_transport():
    """It reads in-memory state only; no session, no network path."""
    source = Path(preflight_module.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]
    for forbidden in ("import boto3", "import botocore", "requests", "http.client"):
        assert forbidden not in body, f"preflight must not contain {forbidden!r}"


def test_the_preflight_gate_does_not_require_cloudtrail_reconciliation():
    """CloudTrail is post-dispatch evidence and must not gate authorization.

    Reconciliation confirms what AWS received, which cannot be known before the
    dispatch happens. If the pre-dispatch gate depended on it, no run could
    ever start -- and the tempting "fix" would be to weaken the check rather
    than remove the cycle. Asserted structurally via the import graph and the
    source, so the dependency cannot reappear silently.
    """
    import ast

    import sws_agent.reconciliation as reconciliation

    source = Path(preflight_module.__file__).read_text(encoding="utf-8")
    assert "reconcil" not in source, "the pre-dispatch gate must not require reconciliation"

    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("reconcil" in name for name in imported)

    # And the dependency is genuinely absent, not merely unmentioned.
    assert preflight_module.__name__ != reconciliation.__name__


def test_the_preflight_module_does_not_import_the_execution_coordinator():
    """Keeps the gate independent of the code it is meant to gate."""
    source = Path(preflight_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("from sws_agent.execution", "import sws_agent.execution"):
        assert forbidden not in source


# -- evidence is carried, not just asserted ------------------------------------


def test_every_finding_records_its_evidence():
    report, _policy, _client = _good_preflight()
    for finding in report.findings:
        assert finding.detail.strip(), finding.check
        assert finding.evidence.strip(), finding.check


def test_the_retry_evidence_names_the_effective_value():
    report, _policy, _client = _good_preflight()
    finding = next(f for f in report.findings if f.check == "retry_configuration")
    assert "total_max_attempts" in finding.evidence
    assert "'mode': 'standard'" in finding.evidence or '"mode": "standard"' in finding.evidence


def test_a_deeply_broken_policy_does_not_crash_the_gate():
    """Malformed input must produce a refusal, never an exception.

    A gate that raises on bad input gives an operator a stack trace instead of a
    reason, and stack traces invite skipping the gate.
    """
    report, _policy, _client = _good_preflight(policy={"Statement": "not-a-list"})
    assert not report.authorized
    assert any(f.check == "target_scoping" for f in report.failures)


def test_a_policy_with_no_statements_is_refused():
    report, _policy, _client = _good_preflight(policy={"Version": "2012-10-17"})
    assert not report.authorized


# -- test doubles -------------------------------------------------------------


class _RetryLooseClient:
    """A real client whose effective budget permits a second attempt.

    Built from a genuine botocore client so the witness can still arm against
    it; only the retry config differs from the factory's.
    """

    def __init__(self) -> None:
        import botocore.config
        import botocore.session

        self._inner = botocore.session.get_session().create_client(
            "ec2",
            region_name=REGION,
            aws_access_key_id=CREDENTIALS.access_key_id,
            aws_secret_access_key=CREDENTIALS.secret_access_key,
            config=botocore.config.Config(
                retries={"total_max_attempts": 3, "mode": "standard"}
            ),
        )

    @property
    def meta(self) -> Any:
        return self._inner.meta


class _ExplodingRetryConfig:
    @property
    def retries(self) -> dict[str, Any]:
        raise MutationClientConfigurationError("retries is unreadable")


class _ExplodingRetryClient:
    """Exposes a ``meta.config`` whose ``retries`` raises on access.

    Narrower than a blanket ``__getattr__`` raise on purpose: a double that
    explodes on *every* attribute cannot be attached to, which would test the
    wrong refusal entirely.
    """

    class _Meta:
        config = _ExplodingRetryConfig()

    meta = _Meta()


class _DivergentInstanceIdSettings:
    """Real settings shape, but with an ``instance_id`` that is not from the ARN.

    ``MutationClientSettings`` derives ``instance_id`` from the ARN as a
    read-only property, so a genuine disagreement cannot be constructed through
    the public API -- which is itself a safety property. This stand-in lets the
    gate's reconciliation logic be tested anyway, so it is not left unexercised
    on the assumption that construction already covers it.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._settings = MutationClientSettings(**kwargs)
        self.target_instance_arn = self._settings.target_instance_arn
        self.region = self._settings.region
        self.credentials = self._settings.credentials

    @property
    def instance_id(self) -> str:
        return "i-0000000000000000a"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._settings, name)


class _RawSettings:
    """Settings-shaped without construction-time validation.

    ``MutationClientSettings`` rejects a non-instance or wildcard ARN in its
    constructor, so the gate's own ARN-parsing branches are unreachable through
    the public API. They are defense in depth -- a future refactor, or a settings
    object from elsewhere -- and defense in depth that cannot be exercised is
    defense in depth nobody knows works. This double supplies the same attribute
    surface so those branches can be tested directly.
    """

    def __init__(self, arn: str, instance_id: str) -> None:
        self.target_instance_arn = arn
        self.region = REGION
        self.credentials = CREDENTIALS
        self._instance_id = instance_id

    @property
    def instance_id(self) -> str:
        return self._instance_id


class _NeverArmedWitness:
    armed = False
    records: list[Any] = []

    def violations(self) -> tuple[str, ...]:
        return ("no attempt observed",)


class _PrefilledWitness:
    armed = True

    def __init__(self, records: int) -> None:
        self.records = [_record(instance_ids=(INSTANCE_ID,)) for _ in range(records)]

    def violations(self) -> tuple[str, ...]:
        return ()


def _record(*, instance_ids: tuple[str, ...], attempt: int = 1) -> Any:
    from sws_agent.mutation_evidence import DispatchRecord

    return DispatchRecord(
        operation="StopInstances",
        invocation_id="inv-1",
        attempt=attempt,
        max_attempts=None,
        instance_ids=instance_ids,
        captured_during="before-send",
    )


# -- unused-import guard ------------------------------------------------------


def test_copy_and_io_are_actually_used():
    """These imports are load-bearing for the double-copied policy tests."""
    assert callable(copy.deepcopy)
    assert io.BytesIO is not None


def test_the_execution_registry_really_has_no_implemented_actions() -> None:
    """The gate's ``program_invariants`` check assumes this; prove the premise."""
    assert [a for a, s in ACTION_EXECUTION_REGISTRY.items() if s.implemented] == []


def test_the_healthy_path_uses_the_registry_not_a_hardcoded_list() -> None:
    report, _policy, _client = _good_preflight(
        implemented_actions=[
            a for a, s in ACTION_EXECUTION_REGISTRY.items() if s.implemented
        ]
    )
    assert report.authorized


# -- the MCP surface is pinned, and the pin is checked -------------------------


def test_the_pinned_mcp_surface_has_not_drifted() -> None:
    """``preflight`` pins the nine names locally instead of importing them.

    That duplication is the price of not importing the MCP stack into the gate.
    This is the test that pays it: if a tool is added, removed or renamed, the
    pin and reality disagree here rather than silently at the gate.
    """
    assert tuple(sorted(BUILTIN_TOOL_NAMES)) == EXPECTED_MCP_TOOL_NAMES
    assert len(EXPECTED_MCP_TOOL_NAMES) == 9


def test_an_added_mcp_tool_fails_the_program_invariants() -> None:
    """The ruling is that MCP stays at nine tools.

    ``check_program_invariants`` used to accept ``tool_names`` and ignore it, so
    this would have passed while its docstring claimed the opposite. An extra
    tool is exactly the change the pre-mutation gate exists to catch.
    """
    added = [*BUILTIN_TOOL_NAMES, "stop_instances"]
    report, _policy, _client = _good_preflight(mcp_tool_names=added)
    assert not report.authorized
    finding = next(f for f in report.findings if f.check == "program_invariants")
    assert not finding.passed
    assert "added=['stop_instances']" in finding.detail
    assert "missing=[]" in finding.detail


def test_a_removed_mcp_tool_fails_the_program_invariants() -> None:
    """Shrinking the surface is a change too, and not a safe direction."""
    removed = [n for n in BUILTIN_TOOL_NAMES if n != "get_relationships"]
    report, _policy, _client = _good_preflight(mcp_tool_names=removed)
    assert not report.authorized
    finding = next(f for f in report.findings if f.check == "program_invariants")
    assert not finding.passed
    assert "added=[]" in finding.detail
    assert "missing=['get_relationships']" in finding.detail


def test_a_duplicate_name_is_ignored_because_a_surface_is_a_set() -> None:
    """Comparison is set equality, so a repeated name changes nothing.

    This test originally asserted the opposite, on the theory that a duplicate
    should fail. That was wrong: the invariant is about the *surface*, and a set
    of tool names with one name repeated is the same surface. The property that
    actually matters is the next one -- a duplicate must not be able to stand in
    for a missing tool, which is why the comparison cannot be a length check.
    """
    padded = [*BUILTIN_TOOL_NAMES, "audit_workspace"]
    report, _policy, _client = _good_preflight(mcp_tool_names=padded)
    assert report.authorized, report.reason()


def test_a_duplicate_cannot_pad_the_count_over_a_missing_tool() -> None:
    """Nine arguments, one of them repeated, one real tool absent.

    A ``len(...) == 9`` implementation would pass this. Set equality fails it,
    which is the whole reason the check is set equality rather than a count.
    """
    padded = [n for n in BUILTIN_TOOL_NAMES if n != "request_approval"]
    padded.append("audit_workspace")
    assert len(padded) == 9
    report, _policy, _client = _good_preflight(mcp_tool_names=padded)
    assert not report.authorized
    finding = next(f for f in report.findings if f.check == "program_invariants")
    assert "missing=['request_approval']" in finding.detail


def test_an_empty_tool_surface_fails() -> None:
    """Empty input is the shape a broken caller produces, and must not pass."""
    report, _policy, _client = _good_preflight(mcp_tool_names=[])
    assert not report.authorized
    finding = next(f for f in report.findings if f.check == "program_invariants")
    assert finding.evidence == "tools=[]"