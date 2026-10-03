"""M11 read-only EC2 observation provider: hermetic tests.

Every test here is credential-free and network-free. No botocore Stubber and
no real boto3 client is used: the provider receives a hand-rolled seam double
whose ``describe_instances()`` returns a fake paginator, so the AWS SDK never
enters the process and ``sws_agent.ec2_observation`` is exercised exactly as
production wires it.

The failure cases matter more than the happy path: M11's safety property is
that no AWS outcome -- missing, ambiguous, denied, throttled, timed out, or
malformed -- can ever be mistaken for a valid "this instance is stopped"
observation. Each of those is asserted to raise ``ObservationError`` instead.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any

import pytest

from sws_agent import ec2_observation
from sws_agent.aws import AwsMultiClient
from sws_agent.constants import SWSResourceType
from sws_agent.ec2_observation import (
    DEFAULT_PARTITION,
    Ec2InstanceObservationProvider,
)
from sws_agent.models import ResourceObservation
from sws_agent.verification import (
    DefaultOutcomeVerifier,
    ObservationError,
    ObservationProvider,
)

REGION = "us-east-1"
ACCOUNT = "123456789012"
INSTANCE_ID = "i-0abc123def4567890"


class FakePaginator:
    """Fake ``ec2:DescribeInstances`` paginator yielding canned pages."""

    def __init__(self, pages: list[Any], *, error: Exception | None = None) -> None:
        self._pages = pages
        self._error = error
        self.paginate_calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> Any:
        self.paginate_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return iter(self._pages)


class FakeEc2Seam:
    """Stand-in for ``AwsMultiClient.describe_instances()``.

    Mirrors the real seam exactly: it takes no arguments and returns something
    with ``.paginate(...)``. ``describe_instances_calls`` counts how many
    logical operations the provider performed.
    """

    def __init__(
        self,
        pages: list[Any] | None = None,
        *,
        paginator_error: Exception | None = None,
        seam_error: Exception | None = None,
    ) -> None:
        self.describe_instances_calls = 0
        self._paginator = FakePaginator(
            list(pages or []), error=paginator_error
        )
        self._seam_error = seam_error

    def describe_instances(self) -> FakePaginator:
        self.describe_instances_calls += 1
        if self._seam_error is not None:
            raise self._seam_error
        return self._paginator

    @property
    def paginate_calls(self) -> list[dict[str, Any]]:
        return self._paginator.paginate_calls


def _instance(
    *,
    instance_id: str | None = INSTANCE_ID,
    state: str | None = "running",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if instance_id is not None:
        payload["InstanceId"] = instance_id
    if state is not None:
        payload["State"] = {"Code": 16, "Name": state}
    payload.update(extra)
    return payload


def _reservation(
    *, owner_id: str | None = ACCOUNT, instances: list[Any] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"Instances": list(instances or [])}
    if owner_id is not None:
        payload["OwnerId"] = owner_id
    return payload


def _page(*reservations: Any) -> dict[str, Any]:
    return {"Reservations": list(reservations)}


def _clock(stamp: datetime) -> Any:
    return lambda: stamp


def _provider(seam: Any, *, region: str = REGION, now: Any = None, **kw: Any):
    return Ec2InstanceObservationProvider(
        seam, region=region, now=now or _clock(datetime(2026, 3, 1, tzinfo=timezone.utc)), **kw
    )


# ---------------------------------------------------------------- happy path


def test_single_instance_happy_path() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    observation = _provider(seam).observe(INSTANCE_ID)
    assert isinstance(observation, ResourceObservation)


def test_observation_reports_the_instance_id_aws_returned() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    assert _provider(seam).observe(INSTANCE_ID).resource_id == INSTANCE_ID


def test_observation_resource_type_is_ec2_instance() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    observation = _provider(seam).observe(INSTANCE_ID)
    assert observation.resource_type is SWSResourceType.EC2_INSTANCE
    assert observation.resource_type.value == "ec2_instance"


def test_observation_arn_is_the_canonical_ec2_instance_arn() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    observation = _provider(seam).observe(INSTANCE_ID)
    assert observation.arn == (
        f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE_ID}"
    )


def test_account_id_comes_from_the_reservation_owner_id() -> None:
    seam = FakeEc2Seam([_page(_reservation(owner_id="210987654321", instances=[_instance()]))])
    observation = _provider(seam).observe(INSTANCE_ID)
    assert observation.account_id == "210987654321"
    assert observation.arn is not None
    assert f":{observation.account_id}:instance/" in observation.arn


def test_region_binding_is_reported_verbatim() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    assert _provider(seam, region="eu-central-1").observe(INSTANCE_ID).region == "eu-central-1"


def test_region_binding_changes_the_constructed_arn() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    observation = _provider(seam, region="ap-northeast-1").observe(INSTANCE_ID)
    assert observation.arn == (
        f"arn:aws:ec2:ap-northeast-1:{ACCOUNT}:instance/{INSTANCE_ID}"
    )


def test_partition_binding_changes_the_constructed_arn() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    observation = _provider(seam, partition="aws-us-gov").observe(INSTANCE_ID)
    assert observation.arn == (
        f"arn:aws-us-gov:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE_ID}"
    )
    assert observation.region == REGION
    assert observation.account_id == ACCOUNT


def test_default_partition_is_the_commercial_partition() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    provider = Ec2InstanceObservationProvider(seam, region=REGION)
    assert provider._partition == DEFAULT_PARTITION
    assert provider.observe(INSTANCE_ID).arn is not None
    assert DEFAULT_PARTITION in (provider.observe(INSTANCE_ID).arn or "")


@pytest.mark.parametrize("state", ["stopped", "running", "stopping", "terminated", "pending"])
def test_state_is_passed_through_verbatim(state: str) -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance(state=state)]))])
    observation = _provider(seam).observe(INSTANCE_ID)
    assert observation.facts["state"] == state


def test_state_is_never_normalized_even_when_aws_sends_odd_casing() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance(state="STOPPED")]))])
    observation = _provider(seam).observe(INSTANCE_ID)
    # No lowercasing, no alias mapping: M10 compares exact string equality
    # against "stopped", so the raw AWS value is the only honest fact.
    assert observation.facts["state"] == "STOPPED"


def test_observation_provenance_is_provider_issued() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    assert _provider(seam).observe(INSTANCE_ID).provenance.value == "provider_issued"


def test_observation_is_not_marked_ambiguous_on_success() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    assert _provider(seam).observe(INSTANCE_ID).ambiguous is False


def test_observed_at_comes_from_the_injected_clock() -> None:
    stamp = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    assert _provider(seam, now=_clock(stamp)).observe(INSTANCE_ID).observed_at == stamp


def test_observed_at_is_timezone_aware_by_default_clock() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    assert Ec2InstanceObservationProvider(seam, region=REGION).observe(
        INSTANCE_ID
    ).observed_at.tzinfo is not None


def test_caller_cannot_supply_observed_at() -> None:
    signature = inspect.signature(Ec2InstanceObservationProvider.observe)
    assert list(signature.parameters) == ["self", "resource_id"]
    with pytest.raises(TypeError):
        Ec2InstanceObservationProvider.observe(  # type: ignore[call-arg]
            object(), INSTANCE_ID, observed_at=datetime.now(timezone.utc)
        )


def test_provider_satisfies_the_m10_observation_provider_protocol() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    assert isinstance(_provider(seam), ObservationProvider)


def test_observe_signature_is_unchanged_from_m10() -> None:
    signature = inspect.signature(Ec2InstanceObservationProvider.observe)
    assert list(signature.parameters) == ["self", "resource_id"]


# ------------------------------------------------------------- optional facts


def test_optional_facts_are_surfaced_when_aws_sends_them() -> None:
    instance = _instance(
        InstanceType="t3.micro",
        LaunchTime=datetime(2026, 2, 1, tzinfo=timezone.utc),
        PrivateIpAddress="10.0.0.5",
        VpcId="vpc-123",
        SubnetId="subnet-123",
        Placement={"AvailabilityZone": "us-east-1a", "Tenancy": "default"},
    )
    seam = FakeEc2Seam([_page(_reservation(instances=[instance]))])
    facts = _provider(seam).observe(INSTANCE_ID).facts
    assert facts["instance_type"] == "t3.micro"
    assert facts["launch_time"] == "2026-02-01T00:00:00+00:00"
    assert facts["private_ip_address"] == "10.0.0.5"
    assert facts["vpc_id"] == "vpc-123"
    assert facts["subnet_id"] == "subnet-123"
    assert facts["availability_zone"] == "us-east-1a"


def test_omitted_optional_attributes_are_absent_not_defaulted() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    facts = _provider(seam).observe(INSTANCE_ID).facts
    assert facts == {"state": "running"}


def test_optional_facts_never_include_unnecessary_ec2_fields() -> None:
    instance = _instance(
        ImageId="ami-123",
        KeyName="my-key",
        EbsOptimized=False,
        Architecture="x86_64",
        StateReason={"Code": "ok"},
        KernelId="aki-123",
    )
    seam = FakeEc2Seam([_page(_reservation(instances=[instance]))])
    facts = _provider(seam).observe(INSTANCE_ID).facts
    for unwanted in (
        "image_id",
        "key_name",
        "ebs_optimized",
        "architecture",
        "state_reason",
        "kernel_id",
    ):
        assert unwanted not in facts


# ------------------------------------------------------------------ pagination


def test_all_paginator_pages_are_drained() -> None:
    pages = [
        _page(_reservation(instances=[])),
        _page(_reservation(instances=[])),
        _page(_reservation(instances=[_instance()])),
    ]
    seam = FakeEc2Seam(pages)
    assert _provider(seam).observe(INSTANCE_ID).resource_id == INSTANCE_ID


def test_exactly_one_logical_client_operation_is_performed() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    _provider(seam).observe(INSTANCE_ID)
    assert seam.describe_instances_calls == 1
    assert len(seam.paginate_calls) == 1


def test_paginator_is_requested_with_the_exact_instance_id() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    _provider(seam).observe(INSTANCE_ID)
    assert seam.paginate_calls == [{"InstanceIds": [INSTANCE_ID]}]


def test_paginator_is_never_called_with_a_filter_or_a_wildcard() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    _provider(seam).observe(INSTANCE_ID)
    (call,) = seam.paginate_calls
    assert "Filters" not in call
    assert "MaxResults" not in call


def test_instances_split_across_reservations_in_one_page_are_all_counted() -> None:
    pages = [
        _page(
            _reservation(instances=[_instance(instance_id="i-1")]),
            _reservation(instances=[_instance(instance_id="i-2")]),
        )
    ]
    with pytest.raises(ObservationError, match="2 instances"):
        _provider(FakeEc2Seam(pages)).observe(INSTANCE_ID)


# -------------------------------------------------------------- zero results


def test_empty_reservations_list_raises() -> None:
    with pytest.raises(ObservationError, match="no visible instance"):
        _provider(FakeEc2Seam([_page()])).observe(INSTANCE_ID)


def test_reservation_with_no_instances_raises() -> None:
    with pytest.raises(ObservationError, match="no visible instance"):
        _provider(FakeEc2Seam([_page(_reservation(instances=[]))])).observe(INSTANCE_ID)


def test_no_pages_at_all_raises() -> None:
    with pytest.raises(ObservationError, match="no visible instance"):
        _provider(FakeEc2Seam([])).observe(INSTANCE_ID)


def test_missing_reservations_key_raises() -> None:
    with pytest.raises(ObservationError, match="no visible instance"):
        _provider(FakeEc2Seam([{}])).observe(INSTANCE_ID)


# ----------------------------------------------------------- multiple results


def test_two_instances_for_one_requested_id_raises() -> None:
    pages = [
        _page(
            _reservation(
                instances=[_instance(instance_id="i-1"), _instance(instance_id="i-2")]
            )
        )
    ]
    with pytest.raises(ObservationError, match="2 instances"):
        _provider(FakeEc2Seam(pages)).observe(INSTANCE_ID)


def test_duplicate_instance_across_pages_raises_rather_than_choosing() -> None:
    pages = [
        _page(_reservation(instances=[_instance(instance_id="i-1")])),
        _page(_reservation(instances=[_instance(instance_id="i-1")])),
    ]
    with pytest.raises(ObservationError, match="2 instances"):
        _provider(FakeEc2Seam(pages)).observe(INSTANCE_ID)


# ------------------------------------------------------------------- malformed


def test_missing_instance_id_raises() -> None:
    pages = [_page(_reservation(instances=[_instance(instance_id=None)]))]
    with pytest.raises(ObservationError, match="instance id"):
        _provider(FakeEc2Seam(pages)).observe(INSTANCE_ID)


def test_blank_instance_id_raises() -> None:
    pages = [_page(_reservation(instances=[_instance(instance_id="   ")]))]
    with pytest.raises(ObservationError, match="instance id"):
        _provider(FakeEc2Seam(pages)).observe(INSTANCE_ID)


def test_non_string_instance_id_raises() -> None:
    pages = [_page(_reservation(instances=[_instance(instance_id=42)]))]
    with pytest.raises(ObservationError, match="instance id"):
        _provider(FakeEc2Seam(pages)).observe(INSTANCE_ID)


def test_missing_state_raises() -> None:
    pages = [_page(_reservation(instances=[_instance(state=None)]))]
    with pytest.raises(ObservationError, match="state"):
        _provider(FakeEc2Seam(pages)).observe(INSTANCE_ID)


def test_missing_state_name_raises() -> None:
    instance = {"InstanceId": INSTANCE_ID, "State": {"Code": 16}}
    with pytest.raises(ObservationError, match="state"):
        _provider(FakeEc2Seam([_page(_reservation(instances=[instance]))])).observe(
            INSTANCE_ID
        )


def test_blank_state_name_raises() -> None:
    with pytest.raises(ObservationError, match="state"):
        _provider(
            FakeEc2Seam([_page(_reservation(instances=[_instance(state="  ")]))])
        ).observe(INSTANCE_ID)


def test_missing_owner_id_raises() -> None:
    pages = [_page(_reservation(owner_id=None, instances=[_instance()]))]
    with pytest.raises(ObservationError, match="account identity"):
        _provider(FakeEc2Seam(pages)).observe(INSTANCE_ID)


@pytest.mark.parametrize("owner_id", ["123", "12345678901234", "abcdefghijkl", "", "12345678901a"])
def test_invalid_owner_id_raises(owner_id: str) -> None:
    pages = [_page(_reservation(owner_id=owner_id, instances=[_instance()]))]
    with pytest.raises(ObservationError, match="account identity"):
        _provider(FakeEc2Seam(pages)).observe(INSTANCE_ID)


def test_non_string_page_raises() -> None:
    with pytest.raises(ObservationError, match="malformed DescribeInstances page"):
        _provider(FakeEc2Seam(["not-a-page"])).observe(INSTANCE_ID)


def test_non_list_reservations_raises() -> None:
    with pytest.raises(ObservationError, match="malformed Reservations"):
        _provider(FakeEc2Seam([{"Reservations": {}}])).observe(INSTANCE_ID)


def test_non_list_instances_raises() -> None:
    page = {"Reservations": [{"OwnerId": ACCOUNT, "Instances": {}}]}
    with pytest.raises(ObservationError, match="malformed Instances"):
        _provider(FakeEc2Seam([page])).observe(INSTANCE_ID)


def test_malformed_reservation_raises() -> None:
    with pytest.raises(ObservationError, match="malformed reservation"):
        _provider(FakeEc2Seam([{"Reservations": ["nope"]}])).observe(INSTANCE_ID)


def test_malformed_instance_raises() -> None:
    page = {"Reservations": [{"OwnerId": ACCOUNT, "Instances": ["nope"]}]}
    with pytest.raises(ObservationError, match="malformed instance"):
        _provider(FakeEc2Seam([page])).observe(INSTANCE_ID)


# ------------------------------------------------------------- AWS exceptions


class _FakeAwsError(Exception):
    """Shaped like a botocore error without importing botocore."""


def test_invalid_instance_id_not_found_raises() -> None:
    seam = FakeEc2Seam(paginator_error=_FakeAwsError("InvalidInstanceID.NotFound"))
    with pytest.raises(ObservationError, match="authoritative state"):
        _provider(seam).observe(INSTANCE_ID)


def test_access_denied_raises() -> None:
    seam = FakeEc2Seam(paginator_error=_FakeAwsError("AccessDeniedException"))
    with pytest.raises(ObservationError, match="authoritative state"):
        _provider(seam).observe(INSTANCE_ID)


def test_unauthorized_operation_raises() -> None:
    seam = FakeEc2Seam(paginator_error=_FakeAwsError("UnauthorizedOperation"))
    with pytest.raises(ObservationError, match="authoritative state"):
        _provider(seam).observe(INSTANCE_ID)


def test_throttling_raises() -> None:
    seam = FakeEc2Seam(paginator_error=_FakeAwsError("RequestLimitExceeded"))
    with pytest.raises(ObservationError, match="authoritative state"):
        _provider(seam).observe(INSTANCE_ID)


def test_transport_failure_raises() -> None:
    seam = FakeEc2Seam(paginator_error=_FakeAwsError("EndpointConnectionError"))
    with pytest.raises(ObservationError, match="authoritative state"):
        _provider(seam).observe(INSTANCE_ID)


def test_read_timeout_raises() -> None:
    seam = FakeEc2Seam(paginator_error=_FakeAwsError("ReadTimeoutError"))
    with pytest.raises(ObservationError, match="authoritative state"):
        _provider(seam).observe(INSTANCE_ID)


def test_seam_failure_raises() -> None:
    seam = FakeEc2Seam(seam_error=_FakeAwsError("no ec2 client factory"))
    with pytest.raises(ObservationError, match="paginator"):
        _provider(seam).observe(INSTANCE_ID)


def test_raw_aws_error_text_is_not_propagated() -> None:
    secretish = "arn:aws:iam::123456789012:user/admin AKIAEXAMPLE session-token-xyz"
    seam = FakeEc2Seam(paginator_error=_FakeAwsError(secretish))
    with pytest.raises(ObservationError) as excinfo:
        _provider(seam).observe(INSTANCE_ID)
    message = str(excinfo.value)
    assert secretish not in message
    assert "AKIAEXAMPLE" not in message
    assert "_FakeAwsError" in message  # the Python type, never the AWS text


def test_provider_does_not_retry_itself() -> None:
    seam = FakeEc2Seam(paginator_error=_FakeAwsError("RequestLimitExceeded"))
    with pytest.raises(ObservationError):
        _provider(seam).observe(INSTANCE_ID)
    # One logical operation: retry behavior stays with the AWS SDK config.
    assert seam.describe_instances_calls == 1
    assert len(seam.paginate_calls) == 1


# -------------------------------------------------------------- bad arguments


@pytest.mark.parametrize("resource_id", ["", "   ", None, 42, b"i-1", [INSTANCE_ID]])
def test_blank_or_non_string_resource_id_raises(resource_id: Any) -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    with pytest.raises(ObservationError, match="non-empty instance id"):
        _provider(seam).observe(resource_id)
    assert seam.describe_instances_calls == 0


@pytest.mark.parametrize("region", ["", "   ", None, 7])
def test_provider_requires_an_explicit_region(region: Any) -> None:
    with pytest.raises(ValueError, match="region binding"):
        Ec2InstanceObservationProvider(FakeEc2Seam(), region=region)


@pytest.mark.parametrize("partition", ["", "   ", None, 7])
def test_provider_requires_an_explicit_partition(partition: Any) -> None:
    with pytest.raises(ValueError, match="partition binding"):
        Ec2InstanceObservationProvider(
            FakeEc2Seam(), region=REGION, partition=partition
        )


# ----------------------------------------------------- invalid observation model


def test_naive_clock_is_translated_to_observation_error() -> None:
    naive = datetime(2026, 3, 1)  # tzinfo=None
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    with pytest.raises(ObservationError, match="valid evidence"):
        _provider(seam, now=_clock(naive)).observe(INSTANCE_ID)


def test_invalid_observation_model_is_translated_to_observation_error() -> None:
    """A fact that cannot be modelled must fail closed, not surface raw."""
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    with pytest.raises(ObservationError, match="ValidationError") as excinfo:
        _provider(seam, now=_clock(object())).observe(INSTANCE_ID)
    assert "AKIA" not in str(excinfo.value)


def test_pydantic_validation_error_never_escapes_as_a_valid_observation() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance()]))])
    with pytest.raises(ObservationError) as excinfo:
        _provider(seam, now=_clock(object())).observe(INSTANCE_ID)
    # The cause is kept for debugging, but only the Python type name is
    # surfaced in the message the coordinator may record.
    assert excinfo.value.__cause__ is not None
    assert type(excinfo.value.__cause__).__name__ == "ValidationError"


# ------------------------------------------------- wrong-target observation


def test_returned_instance_id_differs_from_requested_id_is_reported_as_returned() -> None:
    """The provider must not launder a wrong-target read into a claim.

    M10's identity gate compares ``observation.resource_id`` with the
    requested id, so reporting AWS's id is what makes a target mismatch
    detectable instead of silently accepted.
    """
    other = "i-0fffffffffffffff"
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance(instance_id=other)]))])
    observation = _provider(seam).observe(INSTANCE_ID)
    assert observation.resource_id == other
    assert observation.resource_id != INSTANCE_ID
    assert observation.arn is not None
    assert observation.arn.endswith(f":instance/{other}")


def test_returned_instance_id_is_never_replaced_by_the_requested_id() -> None:
    other = "i-0fffffffffffffff"
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance(instance_id=other)]))])
    assert _provider(seam).observe(INSTANCE_ID).resource_id != INSTANCE_ID


# --------------------------------------------------- M10 postcondition contract


def test_stopped_instance_satisfies_the_m10_stop_postcondition() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance(state="stopped")]))])
    observation = _provider(seam).observe(INSTANCE_ID)
    result = DefaultOutcomeVerifier().verify(
        observation=observation, expected_facts={"state": "stopped"}
    )
    assert result.status.value == "success"


def test_running_instance_does_not_satisfy_the_stop_postcondition() -> None:
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance(state="running")]))])
    observation = _provider(seam).observe(INSTANCE_ID)
    result = DefaultOutcomeVerifier().verify(
        observation=observation, expected_facts={"state": "stopped"}
    )
    assert result.status.value != "success"


def test_terminated_instance_never_satisfies_the_stop_postcondition() -> None:
    """Recently terminated instances stay visible for up to an hour."""
    seam = FakeEc2Seam([_page(_reservation(instances=[_instance(state="terminated")]))])
    observation = _provider(seam).observe(INSTANCE_ID)
    result = DefaultOutcomeVerifier().verify(
        observation=observation, expected_facts={"state": "stopped"}
    )
    assert result.status.value != "success"


def test_ambiguous_states_never_satisfy_the_stop_postcondition() -> None:
    for state in ("stopping", "shutting-down", "pending"):
        seam = FakeEc2Seam([_page(_reservation(instances=[_instance(state=state)]))])
        result = DefaultOutcomeVerifier().verify(
            observation=_provider(seam).observe(INSTANCE_ID),
            expected_facts={"state": "stopped"},
        )
        assert result.status.value != "success", state


# ------------------------------------------------------------- module boundaries


def test_module_never_imports_the_aws_sdk() -> None:
    source = inspect.getsource(ec2_observation)
    for marker in ("import boto3", "import botocore", "from boto3", "from botocore"):
        assert marker not in source


def test_module_contains_no_dynamic_execution_or_attribute_dispatch() -> None:
    source = inspect.getsource(ec2_observation)
    for marker in ("eval(", "exec(", "__import__", "getattr(", "globals(", "locals("):
        assert marker not in source


def test_module_contains_no_mutation_call() -> None:
    source = inspect.getsource(ec2_observation).lower()
    for marker in ("stop_instances", "terminate_instances", "modify_instance"):
        assert marker not in source


def test_provider_exposes_no_mutation_method() -> None:
    provider = _provider(FakeEc2Seam())
    public = {name for name in dir(provider) if not name.startswith("_")}
    assert public == {"observe"}


def test_provider_imports_only_project_modules_and_stdlib() -> None:
    """No absolute sibling/third-party import: the provider is pure project code.

    boto3/botocore confinement is covered separately; this guards the module
    from reaching for an absolute ``sws_agent`` path or a new dependency.
    """
    allowed = {"__future__", "datetime", "typing"}
    for line in inspect.getsource(ec2_observation).splitlines():
        stripped = line.strip()
        if not stripped.startswith(("import ", "from ")):
            continue
        module = stripped.split()[1].split(".")[0]
        assert module in allowed or stripped.startswith("from ."), stripped


def test_seam_method_takes_no_arbitrary_arguments() -> None:
    """No generic EC2 escape hatch: the read seam accepts nothing."""
    signature = inspect.signature(AwsMultiClient.describe_instances)
    kinds = [param.kind for param in signature.parameters.values()]
    assert not [k for k in kinds if k.name.startswith("VAR_")]


def test_multi_client_surface_is_exactly_eight_named_operations() -> None:
    expected = {
        "list_buckets",
        "get_bucket_location",
        "get_bucket_tagging",
        "list_functions",
        "list_tags",
        "get_cost_and_usage",
        "describe_instances",
        # M13-B: resolves a client already bound to another region. It issues no
        # AWS operation itself; it only lets SWS address a region it was asked to.
        "client_for_region",
    }
    public = {
        name
        for name in dir(AwsMultiClient)
        if not name.startswith("_") and callable(getattr(AwsMultiClient, name, None))
    }
    assert public == expected


def test_multi_client_has_no_generic_dispatch_helper() -> None:
    for name in ("call", "invoke", "request", "ec2_method", "client", "get_client"):
        assert not hasattr(AwsMultiClient, name), name


def test_client_for_region_is_not_a_generic_dispatch_helper() -> None:
    """The M13-B resolver must stay a named region lookup, not a ``client(svc)``.

    A generic ``client(service_name)`` escape hatch would turn the read-only
    surface into a way to reach any AWS operation on any service.
    """
    parameters = list(inspect.signature(AwsMultiClient.client_for_region).parameters)
    assert parameters == ["self", "region"]
    assert "service" not in " ".join(parameters)
