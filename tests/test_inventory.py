"""Read-only inventory mapping (S3 + Lambda) with hermetic fake clients.

No network, no AWS, no credentials, no boto3. Collectors are exercised
through constructor-injected in-memory clients with canned responses.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from sws_agent import inventory as inventory_module
from sws_agent.constants import (
    CollectionFailureCategory,
    SWSResourceType,
    TraceEventType,
    TraceStatus,
)
from sws_agent.inventory import (
    INVENTORY_COLLECTORS,
    Ec2InstanceCollector,
    LambdaFunctionCollector,
    S3BucketCollector,
    UnsupportedResourceTypeError,
    collect_all,
    collect_resource_type,
    normalize_limit,
)
from sws_agent.models import ResourceRecord
from sws_agent.trace import TraceRecorder

BUCKET_CREATED = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
PARTITION_OWNER = {"ID": "123456789012", "DisplayName": "OwnerDisplay"}


class NoSuchTagSetError(Exception):
    """Hermetic stand-in for botocore ClientError with code NoSuchTagSet.

    Mimics the ``.response["Error"]["Code"]`` attribute shape that real
    botocore exceptions carry, without importing boto3 in the test suite.
    """

    def __init__(self, bucket: str):
        super().__init__(
            f"An error occurred (NoSuchTagSet) when calling the "
            f"GetBucketTagging operation: bucket '{bucket}' has no tags"
        )
        self.response = {
            "Error": {
                "Code": "NoSuchTagSet",
                "Message": "The TagSet does not exist",
                "BucketName": bucket,
            }
        }


def _bucket(name: str, **overrides) -> dict:
    bucket = {"Name": name, "CreationDate": BUCKET_CREATED}
    bucket.update(overrides)
    return bucket


def _function(name: str, arn: str = "", **overrides) -> dict:
    function = {
        "FunctionName": name,
        "FunctionArn": arn
        or f"arn:aws:lambda:us-east-1:123456789012:function:{name}",
        "Runtime": "python3.12",
        "Handler": "app.handler",
        "MemorySize": 128,
        "Timeout": 3,
        "PackageType": "Zip",
        "Architectures": ["x86_64"],
        "State": "Active",
        "LastModified": "2026-01-02T03:04:05.000+0000",
    }
    function.update(overrides)
    return function


class FakeS3Client:
    """In-memory S3 client with canned responses and call tracking."""

    def __init__(
        self,
        *,
        buckets=None,
        owner=None,
        locations=None,
        location_errors=None,
        tag_sets=None,
        tag_errors=None,
        no_tag_sets=None,
        list_error=None,
    ):
        self._buckets = list(buckets or [])
        self._owner = dict(owner or PARTITION_OWNER)
        self._locations = dict(locations or {})
        self._location_errors = set(location_errors or ())
        self._tag_sets = dict(tag_sets or {})
        self._tag_errors = set(tag_errors or ())
        self._no_tag_sets = set(no_tag_sets or ())
        self._list_error = list_error
        self.list_calls = 0
        self.location_calls: dict[str, int] = {}
        self.tag_calls: dict[str, int] = {}

    def list_buckets(self):
        self.list_calls += 1
        if self._list_error is not None:
            raise self._list_error
        return {"Buckets": self._buckets, "Owner": self._owner}

    def get_bucket_location(self, Bucket):
        self.location_calls[Bucket] = self.location_calls.get(Bucket, 0) + 1
        if Bucket in self._location_errors:
            raise ValueError(f"location denied for {Bucket}")
        return {"LocationConstraint": self._locations.get(Bucket)}

    def get_bucket_tagging(self, Bucket):
        self.tag_calls[Bucket] = self.tag_calls.get(Bucket, 0) + 1
        if Bucket in self._no_tag_sets:
            raise NoSuchTagSetError(Bucket)
        if Bucket in self._tag_errors:
            raise ValueError(f"tag access denied for {Bucket}")
        return {"TagSet": list(self._tag_sets.get(Bucket) or [])}


class FakeLambdaClient:
    """In-memory Lambda client with pagination, tags, and call tracking."""

    def __init__(self, *, pages=None, tags=None, tag_errors=None, list_error=None):
        self._pages = [list(page) for page in (pages or [])]
        self._tags = dict(tags or {})
        self._tag_errors = set(tag_errors or ())
        self._list_error = list_error
        self._page_index = 0
        self.list_calls = 0
        self.tag_calls = 0

    def list_functions(self, Marker=None):
        self.list_calls += 1
        if self._list_error is not None:
            raise self._list_error
        if self._page_index >= len(self._pages):
            return {"Functions": []}
        page = self._pages[self._page_index]
        self._page_index += 1
        response = {"Functions": page}
        if self._page_index < len(self._pages):
            response["NextMarker"] = f"marker-{self._page_index}"
        return response

    def list_tags(self, Resource):
        self.tag_calls += 1
        if Resource in self._tag_errors:
            raise ValueError(f"tag access denied for {Resource}")
        return {"Tags": dict(self._tags.get(Resource) or {})}


def test_inventory_collectors_registered_for_supported_types():
    assert set(INVENTORY_COLLECTORS) == {
        SWSResourceType.S3_BUCKET,
        SWSResourceType.LAMBDA_FUNCTION,
        SWSResourceType.EC2_INSTANCE,
    }


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def test_s3_maps_bucket_record():
    client = FakeS3Client(
        buckets=[_bucket("my-bucket")],
        locations={"my-bucket": None},
        tag_sets={"my-bucket": [{"Key": "Owner", "Value": "alice"}]},
    )
    records = S3BucketCollector(client).collect()
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, ResourceRecord)
    assert record.resource_id == "my-bucket"
    assert record.resource_type is SWSResourceType.S3_BUCKET
    assert record.name == "my-bucket"
    assert record.region == "us-east-1"
    assert record.owner_tag == "alice"
    assert record.metrics == {}
    assert record.raw == {"owner_id": "123456789012", "owner_display_name": "OwnerDisplay"}


def test_s3_region_conversion_none_and_eu():
    client = FakeS3Client(
        buckets=[
            _bucket("a"),
            _bucket("b"),
            _bucket("c"),
        ],
        locations={"a": None, "b": "EU", "c": "us-west-2"},
    )
    records = {r.resource_id: r for r in S3BucketCollector(client).collect()}
    assert records["a"].region == "us-east-1"
    assert records["b"].region == "eu-west-1"
    assert records["c"].region == "us-west-2"


def test_s3_creation_date_preserved():
    client = FakeS3Client(
        buckets=[_bucket("my-bucket")],
        locations={"my-bucket": None},
    )
    record = S3BucketCollector(client).collect()[0]
    assert record.created_at == BUCKET_CREATED


def test_s3_owner_tag_success_and_absent_tag_is_not_failure():
    client = FakeS3Client(
        buckets=[_bucket("owned"), _bucket("untagged")],
        locations={},
        tag_sets={"owned": [{"Key": "Owner", "Value": "alice"}]},
    )
    trace = TraceRecorder()
    records = {r.resource_id: r for r in S3BucketCollector(client, trace=trace).collect()}
    assert records["owned"].owner_tag == "alice"
    assert records["untagged"].owner_tag is None
    assert not [e for e in trace if e.event_type is TraceEventType.INVENTORY_QUERY
                and e.status is TraceStatus.FAILED]


def test_s3_tag_failure_keeps_record_and_traces_failure():
    client = FakeS3Client(
        buckets=[_bucket("ok"), _bucket("denied")],
        locations={},
        tag_errors={"denied"},
    )
    trace = TraceRecorder()
    records = {r.resource_id: r for r in S3BucketCollector(client, trace=trace).collect()}
    assert records["ok"].owner_tag is None  # no tags configured, not an error
    assert records["denied"].owner_tag is None
    assert {r.resource_id for r in records.values()} == {"ok", "denied"}
    failures = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]
    assert len(failures) == 1
    assert failures[0].metadata["resource_id"] == "denied"


def test_s3_no_tag_set_is_empty_tags_and_not_a_failure():
    """Real S3 raises NoSuchTagSet for untagged buckets (NOT an empty TagSet).

    The bucket must be collected with owner_tag=None and NO ENRICHMENT
    failure, so the workspace snapshot is not made partial solely because a
    bucket has no tags.
    """
    client = FakeS3Client(
        buckets=[_bucket("untagged"), _bucket("owned")],
        locations={},
        tag_sets={"owned": [{"Key": "Owner", "Value": "alice"}]},
        no_tag_sets={"untagged"},
    )
    trace = TraceRecorder()
    records = {r.resource_id: r for r in S3BucketCollector(client, trace=trace).collect()}
    assert records["untagged"].owner_tag is None
    assert records["owned"].owner_tag == "alice"
    assert {r.resource_id for r in records.values()} == {"untagged", "owned"}
    assert not [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]


def test_s3_no_tag_set_distinguished_from_other_tag_failures():
    """NoSuchTagSet is treated as absence; AccessDenied stays an ENRICHMENT failure."""
    client = FakeS3Client(
        buckets=[_bucket("untagged"), _bucket("denied")],
        locations={},
        no_tag_sets={"untagged"},
        tag_errors={"denied"},
    )
    trace = TraceRecorder()
    records = {r.resource_id: r for r in S3BucketCollector(client, trace=trace).collect()}
    assert {r.resource_id for r in records.values()} == {"untagged", "denied"}
    assert all(r.owner_tag is None for r in records.values())
    failures = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]
    assert len(failures) == 1  # only the access-denied bucket failed
    assert failures[0].metadata["resource_id"] == "denied"
    assert (
        failures[0].metadata["category"]
        == CollectionFailureCategory.ENRICHMENT.value
    )


def test_s3_location_failure_keeps_record_with_none_region():
    client = FakeS3Client(
        buckets=[_bucket("unlocatable")],
        locations={},
        location_errors={"unlocatable"},
    )
    trace = TraceRecorder()
    record = S3BucketCollector(client, trace=trace).collect()[0]
    assert record.region is None
    assert record.resource_id == "unlocatable"
    failures = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]
    assert len(failures) == 1
    assert failures[0].metadata["resource_id"] == "unlocatable"


def test_s3_primary_list_failure_returns_empty_and_traces_failure():
    client = FakeS3Client(buckets=[_bucket("x")], list_error=RuntimeError("boom"))
    trace = TraceRecorder()
    assert S3BucketCollector(client, trace=trace).collect() == []
    failures = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]
    assert len(failures) == 1


# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------


def test_lambda_region_extracted_from_arn():
    client = FakeLambdaClient(
        pages=[
            [
                _function("in-eu", "arn:aws:lambda:eu-west-1:123456789012:function:in-eu"),
                _function("in-ap", "arn:aws:lambda:ap-south-1:123456789012:function:in-ap"),
            ]
        ]
    )
    records = {r.resource_id: r for r in LambdaFunctionCollector(client, regions=["eu-west-1", "ap-south-1"]).collect()}
    assert records["arn:aws:lambda:eu-west-1:123456789012:function:in-eu"].region == "eu-west-1"
    assert records["arn:aws:lambda:ap-south-1:123456789012:function:in-ap"].region == "ap-south-1"


def test_lambda_created_at_is_none_and_last_modified_not_mislabeled():
    client = FakeLambdaClient(
        pages=[[_function("fn", "arn:aws:lambda:us-east-1:123456789012:function:fn")]]
    )
    record = LambdaFunctionCollector(client, regions=["us-east-1"]).collect()[0]
    assert record.created_at is None
    assert record.raw["last_modified"] == "2026-01-02T03:04:05.000+0000"


def test_lambda_owner_tag_success_and_failure():
    arn = "arn:aws:lambda:us-east-1:123456789012:function:fn"
    denied_arn = "arn:aws:lambda:us-east-1:123456789012:function:denied"
    client = FakeLambdaClient(
        pages=[[_function("fn", arn), _function("denied", denied_arn)]],
        tags={arn: {"Owner": "bob"}},
        tag_errors={denied_arn},
    )
    trace = TraceRecorder()
    records = {r.resource_id: r for r in LambdaFunctionCollector(client, regions=["us-east-1"], trace=trace).collect()}
    assert records[arn].owner_tag == "bob"
    assert records[denied_arn].owner_tag is None
    failures = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]
    assert len(failures) == 1
    assert failures[0].metadata["resource_id"] == denied_arn


def test_lambda_untagged_function_is_not_a_failure():
    """Untagged functions return an empty Tags map (no NoSuchTagSet analog).

    The function must be collected with owner_tag=None and NO FAILED
    inventory event, so the workspace snapshot is not made partial solely
    because a function has no tags.
    """
    arn = "arn:aws:lambda:us-east-1:123456789012:function:untagged"
    client = FakeLambdaClient(pages=[[_function("untagged", arn)]])
    trace = TraceRecorder()
    records = LambdaFunctionCollector(client, regions=["us-east-1"], trace=trace).collect()
    assert [r.resource_id for r in records] == [arn]
    assert records[0].owner_tag is None
    assert not [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]


def test_lambda_list_tags_uses_real_sdk_resource_keyword():
    """Regression: the collector must call list_tags with ``Resource``.

    Real boto3 ``list_tags`` has no ``Target`` parameter; this strict fake
    would blow up on any old-style call, guaranteeing the recovery of the
    Lambda path against a real Lambda client.
    """
    arn = "arn:aws:lambda:us-east-1:123456789012:function:fn"

    class StrictLambdaClient:
        def __init__(self):
            self.calls = []

        def list_functions(self, Marker=None):
            return {"Functions": [_function("fn", arn)]}

        def list_tags(self, **kwargs):
            self.calls.append(kwargs)
            assert "Target" not in kwargs, "list_tags must not use old Target kwarg"
            assert "Resource" in kwargs, "list_tags must use real SDK Resource kwarg"
            return {"Tags": {"Owner": "carol"}}

    client = StrictLambdaClient()
    records = LambdaFunctionCollector(client, regions=["us-east-1"]).collect()
    assert records[0].owner_tag == "carol"
    assert client.calls == [{"Resource": arn}]


def test_lambda_environment_never_in_raw():
    fn = _function(
        "secretive",
        "arn:aws:lambda:us-east-1:123456789012:function:secretive",
        Environment={"Variables": {"API_SECRET": "do-not-expose"}},
        VpcConfig={"SubnetIds": ["subnet-1"], "SecurityGroupIds": ["sg-1"]},
        Layers=[{"Arn": "arn:aws:lambda:us-east-1:123456789012:layer:util:1"}],
    )
    client = FakeLambdaClient(pages=[[fn]])
    record = LambdaFunctionCollector(client, regions=["us-east-1"]).collect()[0]
    assert "Environment" not in record.raw
    assert set(record.raw) == {
        "runtime",
        "handler",
        "memory_mb",
        "timeout_seconds",
        "package_type",
        "architectures",
        "state",
        "last_modified",
        "vpc_enabled",
        "layered",
    }
    assert record.raw["vpc_enabled"] is True
    assert record.raw["layered"] is True
    assert record.raw["architectures"] == ["x86_64"]


def test_lambda_pagination_continues_to_next_marker():
    client = FakeLambdaClient(
        pages=[
            [_function("f1"), _function("f2")],
            [_function("f3"), _function("f4")],
            [_function("f5")],
        ]
    )
    records = LambdaFunctionCollector(client, regions=["us-east-1"]).collect()
    assert [r.name for r in records] == ["f1", "f2", "f3", "f4", "f5"]
    assert client.list_calls == 3


def test_lambda_limit_enforces_early_stop_without_extra_calls():
    pages = [
        [_function("f1"), _function("f2")],
        [_function("f3"), _function("f4")],
        [_function("f5")],
    ]
    client = FakeLambdaClient(pages=pages)
    records = LambdaFunctionCollector(client, regions=["us-east-1"]).collect(limit=2)
    assert [r.name for r in records] == ["f1", "f2"]
    assert client.list_calls == 1

    client = FakeLambdaClient(pages=pages)
    records = LambdaFunctionCollector(client, regions=["us-east-1"]).collect(limit=3)
    assert [r.name for r in records] == ["f1", "f2", "f3"]
    assert client.list_calls == 2


def test_lambda_truncation_metadata_on_mid_page_and_next_marker_stops():
    def succeeded(trace):
        return [
            e for e in trace
            if e.event_type is TraceEventType.INVENTORY_QUERY
            and e.status is TraceStatus.SUCCEEDED
        ][0]

    mid_page = FakeLambdaClient(pages=[[_function("f1"), _function("f2"), _function("f3")]])
    trace = TraceRecorder()
    LambdaFunctionCollector(mid_page, regions=["us-east-1"], trace=trace).collect(limit=2)
    assert succeeded(trace).metadata.get("truncated") is True

    next_marker = FakeLambdaClient(
        pages=[[_function("f1"), _function("f2")], [_function("f3"), _function("f4")]]
    )
    trace = TraceRecorder()
    LambdaFunctionCollector(next_marker, regions=["us-east-1"], trace=trace).collect(limit=2)
    assert succeeded(trace).metadata.get("truncated") is True


def test_lambda_exact_count_with_no_more_data_is_not_truncated():
    client = FakeLambdaClient(pages=[[_function("f1"), _function("f2")]])
    trace = TraceRecorder()
    records = LambdaFunctionCollector(client, regions=["us-east-1"], trace=trace).collect(limit=2)
    assert [r.name for r in records] == ["f1", "f2"]
    succeed = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.SUCCEEDED
    ][0]
    assert "truncated" not in succeed.metadata


def test_lambda_full_consumption_is_never_truncated():
    client = FakeLambdaClient(
        pages=[
            [_function("f1"), _function("f2")],
            [_function("f3"), _function("f4")],
        ]
    )
    trace = TraceRecorder()
    records = LambdaFunctionCollector(client, regions=["us-east-1"], trace=trace).collect(limit=4)
    assert [r.name for r in records] == ["f1", "f2", "f3", "f4"]
    succeed = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.SUCCEEDED
    ][0]
    assert "truncated" not in succeed.metadata


def test_lambda_primary_list_failure_returns_empty_fail_closed():
    client = FakeLambdaClient(
        pages=[[_function("f1")]],
        list_error=RuntimeError("boom"),
    )
    trace = TraceRecorder()
    collector = LambdaFunctionCollector(client, regions=["us-east-1"], trace=trace)
    assert collector.collect() == []
    failures = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]
    assert len(failures) == 1


def test_lambda_requires_explicit_regions():
    client = FakeLambdaClient(pages=[[_function("f1")]])
    with pytest.raises(ValueError):
        LambdaFunctionCollector(client)
    with pytest.raises(ValueError):
        LambdaFunctionCollector(client, regions=[])
    with pytest.raises(ValueError):
        LambdaFunctionCollector(client, regions=[""])


def test_lambda_malformed_arn_skipped_with_failure_trace():
    bad_no_arn = _function("noarn")
    del bad_no_arn["FunctionArn"]
    client = FakeLambdaClient(
        pages=[
            [
                _function("good", "arn:aws:lambda:us-east-1:123456789012:function:good"),
                _function("bad", "not-an-arn"),
                bad_no_arn,
            ]
        ]
    )
    trace = TraceRecorder()
    records = LambdaFunctionCollector(client, regions=["us-east-1"], trace=trace).collect()
    assert [r.resource_id for r in records] == ["arn:aws:lambda:us-east-1:123456789012:function:good"]
    failures = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]
    assert len(failures) == 2


# ---------------------------------------------------------------------------
# Limits, dispatch, trace counts
# ---------------------------------------------------------------------------


def test_limit_validation_rejects_zero_and_invalid_values():
    client = FakeS3Client(buckets=[_bucket("b")])
    collector = S3BucketCollector(client)
    with pytest.raises(ValueError):
        collector.collect(limit=0)
    with pytest.raises(ValueError):
        collector.collect(limit=-1)
    with pytest.raises(ValueError):
        collector.collect(limit="3")
    with pytest.raises(ValueError):
        collector.collect(limit=True)
    assert len(collector.collect(limit=5)) == 1
    assert len(collector.collect(limit=None)) == 1


def test_s3_limit_stops_enrichment_too():
    buckets = [_bucket(f"bucket-{i}") for i in range(5)]
    client = FakeS3Client(buckets=buckets, locations={}, tag_sets={})
    records = S3BucketCollector(client).collect(limit=2)
    assert [r.resource_id for r in records] == ["bucket-0", "bucket-1"]
    assert set(client.location_calls) == {"bucket-0", "bucket-1"}
    assert set(client.tag_calls) == {"bucket-0", "bucket-1"}


def test_s3_truncation_metadata_only_when_more_buckets_than_limit():
    def succeeded(trace):
        return [
            e for e in trace
            if e.event_type is TraceEventType.INVENTORY_QUERY
            and e.status is TraceStatus.SUCCEEDED
        ][0]

    buckets = [_bucket(f"bucket-{i}") for i in range(3)]
    padded = FakeS3Client(buckets=buckets, locations={})
    trace = TraceRecorder()
    assert len(S3BucketCollector(padded, trace=trace).collect(limit=2)) == 2
    assert succeeded(trace).metadata.get("truncated") is True

    exact = FakeS3Client(buckets=buckets[:2], locations={})
    trace = TraceRecorder()
    assert len(S3BucketCollector(exact, trace=trace).collect(limit=2)) == 2
    assert "truncated" not in succeeded(trace).metadata

    roomy = FakeS3Client(buckets=buckets, locations={})
    trace = TraceRecorder()
    assert len(S3BucketCollector(roomy, trace=trace).collect(limit=3)) == 3
    assert "truncated" not in succeeded(trace).metadata


def test_s3_exact_count_with_no_extra_buckets_is_not_truncated():
    client = FakeS3Client(buckets=[_bucket("a"), _bucket("b")], locations={})
    trace = TraceRecorder()
    records = S3BucketCollector(client, trace=trace).collect(limit=2)
    assert [r.resource_id for r in records] == ["a", "b"]
    succeed = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.SUCCEEDED
    ][0]
    assert "truncated" not in succeed.metadata


def test_trace_records_start_and_success_with_exact_count():
    client = FakeS3Client(
        buckets=[_bucket("a"), _bucket("b"), _bucket("c")], locations={}
    )
    trace = TraceRecorder()
    S3BucketCollector(client, trace=trace).collect()
    statuses = [e.status for e in trace if e.event_type is TraceEventType.INVENTORY_QUERY]
    assert TraceStatus.RUNNING in statuses
    successes = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.SUCCEEDED
    ]
    assert len(successes) == 1
    assert successes[0].metadata["count"] == 3


def test_empty_inventory_succeeds_with_count_zero():
    client = FakeS3Client(buckets=[])
    trace = TraceRecorder()
    assert S3BucketCollector(client, trace=trace).collect() == []
    successes = [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.SUCCEEDED
    ]
    assert successes[0].metadata["count"] == 0


def test_collect_all_returns_supported_types_in_order():
    s3 = FakeS3Client(buckets=[_bucket("my-bucket")], locations={})
    lamb = FakeLambdaClient(
        pages=[[_function("fn", "arn:aws:lambda:us-east-1:123456789012:function:fn")]]
    )

    class FakeRootClient:
        def __init__(self, s3, lamb):
            self._s3 = s3
            self._lamb = lamb

        def list_buckets(self):
            return self._s3.list_buckets()

        def get_bucket_location(self, Bucket):
            return self._s3.get_bucket_location(Bucket)

        def get_bucket_tagging(self, Bucket):
            return self._s3.get_bucket_tagging(Bucket)

        def list_functions(self, Marker=None):
            return self._lamb.list_functions(Marker)

        def list_tags(self, Resource):
            return self._lamb.list_tags(Resource)

    recorder = TraceRecorder()
    results = collect_all(
        client=FakeRootClient(s3, lamb), regions=["us-east-1"], trace=recorder
    )
    assert list(results) == [SWSResourceType.S3_BUCKET, SWSResourceType.LAMBDA_FUNCTION]
    assert [r.resource_type for r in results[SWSResourceType.S3_BUCKET]] == [
        SWSResourceType.S3_BUCKET
    ]
    assert len(results[SWSResourceType.LAMBDA_FUNCTION]) == 1
    # Clean collection: the Lambda tag lookup must succeed with the real
    # ``Resource`` keyword (no swallowed TypeError surfaced as ENRICHMENT).
    assert [
        e for e in recorder
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ] == []


def test_unsupported_resource_types_raise():
    client = FakeS3Client(buckets=[])
    for resource_type in (
        SWSResourceType.EBS_VOLUME,
        SWSResourceType.COST_DATA,
    ):
        with pytest.raises(UnsupportedResourceTypeError):
            collect_resource_type(resource_type, client=client)


def test_ec2_collector_is_registered_but_not_in_the_default_run():
    """Registration and default scope are different claims.

    ``INVENTORY_COLLECTORS`` says "SWS implements inventory for this type".
    ``collect_all`` says "this run read this type". EC2 must satisfy the first
    and be absent from the second unless asked for, so a caller can never read
    an unrequested EC2 pass as "the account has no instances".
    """
    assert SWSResourceType.EC2_INSTANCE in INVENTORY_COLLECTORS
    results = collect_all(
        client=FakeS3Client(buckets=[]), regions=["us-east-1"], trace=TraceRecorder()
    )
    assert SWSResourceType.EC2_INSTANCE not in results


def _failed_events(trace):
    return [
        e for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]


def test_failure_events_carry_primary_category_for_s3_list_failure():
    client = FakeS3Client(buckets=[_bucket("x")], list_error=RuntimeError("boom"))
    trace = TraceRecorder()
    assert S3BucketCollector(client, trace=trace).collect() == []
    failures = _failed_events(trace)
    assert len(failures) == 1
    assert failures[0].metadata["category"] == CollectionFailureCategory.PRIMARY.value


def test_failure_events_carry_enrichment_category_for_s3_lookups():
    client = FakeS3Client(
        buckets=[_bucket("denied")],
        locations={},
        location_errors={"denied"},
        tag_errors={"denied"},
    )
    trace = TraceRecorder()
    S3BucketCollector(client, trace=trace).collect()
    failures = _failed_events(trace)
    assert len(failures) == 2
    assert all(
        f.metadata["category"] == CollectionFailureCategory.ENRICHMENT.value
        for f in failures
    )
    assert {f.metadata["resource_id"] for f in failures} == {"denied"}


def test_failure_events_carry_primary_category_for_lambda_list_failure():
    client = FakeLambdaClient(pages=[[_function("f1")]], list_error=RuntimeError("boom"))
    trace = TraceRecorder()
    assert LambdaFunctionCollector(client, regions=["us-east-1"], trace=trace).collect() == []
    failures = _failed_events(trace)
    assert len(failures) == 1
    assert failures[0].metadata["category"] == CollectionFailureCategory.PRIMARY.value


def test_failure_events_carry_parse_category_for_lambda_skips():
    bad_no_arn = _function("noarn")
    del bad_no_arn["FunctionArn"]
    client = FakeLambdaClient(
        pages=[[_function("malformed", "not-an-arn"), bad_no_arn]]
    )
    trace = TraceRecorder()
    LambdaFunctionCollector(client, regions=["us-east-1"], trace=trace).collect()
    failures = _failed_events(trace)
    assert len(failures) == 2
    assert all(
        f.metadata["category"] == CollectionFailureCategory.PARSE.value
        for f in failures
    )


def test_failure_events_carry_enrichment_category_for_lambda_tags():
    arn = "arn:aws:lambda:us-east-1:123456789012:function:denied"
    client = FakeLambdaClient(
        pages=[[_function("denied", arn)]],
        tag_errors={arn},
    )
    trace = TraceRecorder()
    LambdaFunctionCollector(client, regions=["us-east-1"], trace=trace).collect()
    failures = _failed_events(trace)
    assert len(failures) == 1
    assert (
        failures[0].metadata["category"]
        == CollectionFailureCategory.ENRICHMENT.value
    )
    assert failures[0].metadata["resource_id"] == arn


def test_normalize_limit_public_routine_defaults_validates_passthrough():
    assert normalize_limit(None) == 100
    assert normalize_limit(5) == 5
    assert normalize_limit(5000) == 5000  # positive limits pass through unchanged
    for bad in (0, -1, "5", True, 3.5):
        with pytest.raises(ValueError):
            normalize_limit(bad)


# ---------------------------------------------------------------------------
# EC2 (M13-A) — read-only, one DescribeInstances pass, no mutating operation
# ---------------------------------------------------------------------------

ACCOUNT = "123456789012"
LAUNCH_TIME = datetime(2026, 2, 1, 9, 30, tzinfo=timezone.utc)


class FakeEc2Paginator:
    """Fake ``ec2:DescribeInstances`` paginator yielding canned pages."""

    def __init__(self, pages, *, error=None):
        self._pages = list(pages)
        self._error = error
        self.paginate_calls = 0

    def paginate(self, **kwargs):
        self.paginate_calls += 1
        if self._error is not None:
            raise self._error
        return iter(self._pages)


class FakeEc2Client:
    """Stand-in for the ``AwsMultiClient.describe_instances()`` seam.

    Mirrors the real seam: it takes no arguments and returns something with
    ``.paginate(...)``. Every page handed out is recorded so a test can assert
    how far the collector actually drained.
    """

    def __init__(self, pages=None, *, paginator_error=None, seam_error=None):
        self.describe_instances_calls = 0
        self._paginators = []
        self._page_sets = [list(pages or [])]
        self._paginator_error = paginator_error
        self._seam_error = seam_error

    def set_pages(self, pages):
        """Replace the pages the *next* ``describe_instances()`` call yields."""
        self._page_sets.append(list(pages))

    def describe_instances(self):
        self.describe_instances_calls += 1
        if self._seam_error is not None:
            raise self._seam_error
        pages = self._page_sets.pop(0) if self._page_sets else []
        paginator = FakeEc2Paginator(pages, error=self._paginator_error)
        self._paginators.append(paginator)
        return paginator


def _instance(
    instance_id="i-0abc123def4567890",
    state="running",
    **extra,
):
    payload = {}
    if instance_id is not None:
        payload["InstanceId"] = instance_id
    if state is not None:
        payload["State"] = {"Code": 16, "Name": state}
    payload.update(extra)
    return payload


def _reservation(owner_id=ACCOUNT, instances=None):
    payload = {"Instances": list(instances or [])}
    if owner_id is not None:
        payload["OwnerId"] = owner_id
    return payload


def _page(*reservations):
    return {"Reservations": list(reservations)}


def _ec2(client, *, regions=("us-east-1",), trace=None, limit=None):
    return Ec2InstanceCollector(
        client, regions=list(regions), trace=trace
    ).collect(limit=limit)


def _succeeded_events(trace):
    return [
        e
        for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.SUCCEEDED
    ]


def test_ec2_maps_instance_record():
    client = FakeEc2Client([_page(_reservation(instances=[_instance()]))])
    records = _ec2(client)
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, ResourceRecord)
    assert record.resource_id == "i-0abc123def4567890"
    assert record.resource_type is SWSResourceType.EC2_INSTANCE
    assert record.name == "i-0abc123def4567890"
    assert record.region == "us-east-1"
    assert record.state == "running"
    assert record.metrics == {}


def test_ec2_reads_account_identity_from_the_reservation_not_the_instance():
    """``OwnerId`` lives on the reservation, so flattening must carry it."""
    client = FakeEc2Client([_page(_reservation(instances=[_instance()]))])
    assert _ec2(client)[0].account_id == ACCOUNT


def test_ec2_never_invents_an_account_when_owner_id_is_absent_or_malformed():
    for owner_id in (None, "", "12345", "abcdefghijkl", 123456789012):
        client = FakeEc2Client(
            [_page(_reservation(owner_id=owner_id, instances=[_instance()]))]
        )
        records = _ec2(client)
        assert len(records) == 1, owner_id  # the instance is still identified
        assert records[0].account_id is None, owner_id


def test_ec2_absent_owner_id_is_not_reported_as_a_collection_failure():
    """The instance is fully identified without an account, so nothing failed."""
    trace = TraceRecorder()
    client = FakeEc2Client(
        [_page(_reservation(owner_id=None, instances=[_instance()]))]
    )
    _ec2(client, trace=trace)
    assert _failed_events(trace) == []
    assert _succeeded_events(trace)[0].metadata["count"] == 1


def test_ec2_leaves_owner_tag_none_and_fabricates_no_tag_lookup():
    """The collector has no tag API, so an absent tag is not a finding.

    ``owner_tag`` stays ``None`` because no lookup was attempted -- and
    because attempting-and-failing would have been dishonest, no ENRICHMENT
    failure is traced either. Policy reads this through
    ``OWNER_TAG_COLLECTED_RESOURCE_TYPES`` instead of inferring it.
    """
    trace = TraceRecorder()
    client = FakeEc2Client([_page(_reservation(instances=[_instance()]))])
    record = _ec2(client, trace=trace)[0]
    assert record.owner_tag is None
    assert _failed_events(trace) == []
    source = inspect.getsource(Ec2InstanceCollector)
    assert "describe_tags" not in source
    assert "create_tags" not in source


def test_ec2_does_not_construct_an_arn():
    """``DescribeInstances`` returns no ARN; M11 owns that construction rule."""
    client = FakeEc2Client([_page(_reservation(instances=[_instance()]))])
    assert _ec2(client)[0].arn is None


def test_ec2_state_is_passed_through_byte_for_byte():
    """The collector records; it never decides what a state means.

    Exact-string pass-through is what lets the policy engine reject a state it
    does not recognize instead of the collector quietly normalizing one into a
    neighbour.
    """
    for state in ("running", "pending", "stopping", "RUNNING", "running "):
        client = FakeEc2Client([_page(_reservation(instances=[_instance(state=state)]))])
        assert _ec2(client)[0].state == state, state


def test_ec2_records_no_state_rather_than_a_default():
    for state in (None, "", "   "):
        client = FakeEc2Client([_page(_reservation(instances=[_instance(state=state)]))])
        assert _ec2(client)[0].state is None, state


def test_ec2_missing_state_block_records_no_state():
    client = FakeEc2Client(
        [
            _page(
                _reservation(
                    instances=[{"InstanceId": "i-nostate", "State": {"Code": 0}}]
                )
            )
        ]
    )
    assert _ec2(client)[0].state is None


def test_ec2_uses_aware_launch_time_as_created_at():
    client = FakeEc2Client(
        [_page(_reservation(instances=[_instance(LaunchTime=LAUNCH_TIME)]))]
    )
    assert _ec2(client)[0].created_at == LAUNCH_TIME


def test_ec2_naive_launch_time_never_becomes_an_authoritative_timestamp():
    """A naive timestamp is ambiguous, so ``created_at`` stays None.

    ``canonicalize_resources`` rejects naive timestamps outright, so accepting
    one here would abort an entire workspace collection. The observed value is
    still preserved in ``raw`` rather than being discarded.
    """
    naive = datetime(2026, 2, 1, 9, 30)
    client = FakeEc2Client(
        [_page(_reservation(instances=[_instance(LaunchTime=naive)]))]
    )
    record = _ec2(client)[0]
    assert record.created_at is None
    assert record.raw["launch_time"] == naive.isoformat()


def test_ec2_non_datetime_launch_time_is_ignored_not_raised():
    client = FakeEc2Client(
        [_page(_reservation(instances=[_instance(LaunchTime="2026-02-01")]))]
    )
    assert _ec2(client)[0].created_at is None


def test_ec2_raw_carries_only_the_approved_attribute_set():
    client = FakeEc2Client(
        [
            _page(
                _reservation(
                    instances=[
                        _instance(
                            InstanceType="t3.micro",
                            VpcId="vpc-1",
                            SubnetId="subnet-1",
                            PrivateIpAddress="10.0.0.4",
                            Placement={"AvailabilityZone": "us-east-1a"},
                            # Fields SWS is not authorized to surface.
                            KeyName="prod-key",
                            SecurityGroups=[{"GroupId": "sg-1"}],
                            UserData="#!/bin/sh\n",
                        )
                    ]
                )
            )
        ]
    )
    raw = _ec2(client)[0].raw
    assert raw["instance_type"] == "t3.micro"
    assert raw["vpc_id"] == "vpc-1"
    assert raw["subnet_id"] == "subnet-1"
    assert raw["private_ip_address"] == "10.0.0.4"
    assert raw["availability_zone"] == "us-east-1a"
    assert raw["state_code"] == 16
    for leaked in ("KeyName", "SecurityGroups", "UserData", "key_name", "user_data"):
        assert leaked not in raw, leaked


def test_ec2_records_state_code_only_when_aws_sent_it():
    client = FakeEc2Client(
        [_page(_reservation(instances=[{"InstanceId": "i-x", "State": {"Name": "running"}}]))]
    )
    assert "state_code" not in _ec2(client)[0].raw


def test_ec2_skips_instances_without_a_usable_instance_id():
    for bad in (None, "", "   "):
        trace = TraceRecorder()
        client = FakeEc2Client([_page(_reservation(instances=[_instance(instance_id=bad)]))])
        assert _ec2(client, trace=trace) == []
        failures = _failed_events(trace)
        assert len(failures) == 1
        assert failures[0].metadata["category"] == CollectionFailureCategory.PARSE.value


def test_ec2_skips_a_malformed_instance_item():
    trace = TraceRecorder()
    client = FakeEc2Client([_page(_reservation(instances=["not-a-dict"]))])
    assert _ec2(client, trace=trace) == []
    assert _failed_events(trace)[0].metadata["category"] == (
        CollectionFailureCategory.PARSE.value
    )


def test_ec2_keeps_good_instances_and_skips_only_the_unidentifiable_ones():
    trace = TraceRecorder()
    client = FakeEc2Client(
        [_page(_reservation(instances=[_instance(), _instance(instance_id=None)]))]
    )
    records = _ec2(client, trace=trace)
    assert [r.resource_id for r in records] == ["i-0abc123def4567890"]
    assert len(_failed_events(trace)) == 1


def test_ec2_paginator_failure_is_a_primary_failure():
    trace = TraceRecorder()
    client = FakeEc2Client([_page()], paginator_error=RuntimeError("boom"))
    assert _ec2(client, trace=trace) == []
    failures = _failed_events(trace)
    assert len(failures) == 1
    assert failures[0].metadata["category"] == CollectionFailureCategory.PRIMARY.value


def test_ec2_missing_seam_is_a_primary_failure_not_a_crash():
    """A client without ``describe_instances`` fails the collection honestly."""
    trace = TraceRecorder()
    assert _ec2(FakeS3Client(buckets=[]), trace=trace) == []
    assert _failed_events(trace)[0].metadata["category"] == (
        CollectionFailureCategory.PRIMARY.value
    )


def test_ec2_malformed_page_is_a_primary_failure_never_an_empty_one():
    for page in ("not-a-dict", {"Reservations": "not-a-list"}, {"Reservations": ["x"]}):
        trace = TraceRecorder()
        client = FakeEc2Client([page])
        assert _ec2(client, trace=trace) == [], page
        assert _failed_events(trace)[0].metadata["category"] == (
            CollectionFailureCategory.PRIMARY.value
        )


def test_ec2_requires_an_explicit_region_list():
    for regions in (None, [], [""], [None]):
        with pytest.raises(ValueError, match="region"):
            Ec2InstanceCollector(FakeEc2Client(), regions=regions)


def test_ec2_drains_every_page_of_its_single_region():
    client = FakeEc2Client(
        [
            _page(_reservation(instances=[_instance("i-1")])),
            _page(_reservation(instances=[_instance("i-2")])),
            _page(),
        ]
    )
    records = _ec2(client, regions=["us-east-1"])
    assert [r.resource_id for r in records] == ["i-1", "i-2"]
    assert {r.region for r in records} == {"us-east-1"}
    assert client.describe_instances_calls == 1


def test_ec2_refuses_a_multi_region_request_instead_of_mislabeling_instances():
    """The seam is bound to one region, so a loop would fabricate locations.

    ``AwsMultiClient.describe_instances()`` takes no region argument. Iterating
    ``regions`` against it re-reads the same configured region and stamps each
    copy with a different ``region``, so one instance appears twice under two
    different locations -- and the second region is never actually read at all.
    """
    client = FakeEc2Client([_page(_reservation(instances=[_instance("i-1")]))])
    with pytest.raises(ValueError, match="exactly one region"):
        Ec2InstanceCollector(client, regions=["us-east-1", "us-west-2"])
    assert client.describe_instances_calls == 0
    with pytest.raises(ValueError, match="exactly one region"):
        _ec2(client, regions=["us-east-1", "eu-west-1", "ap-south-1"])


def test_ec2_empty_account_succeeds_with_count_zero():
    trace = TraceRecorder()
    client = FakeEc2Client([{"Reservations": []}])
    assert _ec2(client, trace=trace) == []
    successes = _succeeded_events(trace)
    assert successes[0].metadata["count"] == 0
    assert "truncated" not in successes[0].metadata


def test_ec2_reaching_the_limit_on_the_final_page_is_not_truncation():
    """The exact-limit-on-last-page case must never claim truncated data.

    ``count == limit`` is the shape that a naive implementation would report
    as truncation; doing so would downgrade every policy decision built on the
    snapshot for a workspace that was in fact read completely.
    """
    trace = TraceRecorder()
    client = FakeEc2Client(
        [_page(_reservation(instances=[_instance("i-1"), _instance("i-2")]))]
    )
    assert len(_ec2(client, trace=trace, limit=2)) == 2
    assert "truncated" not in _succeeded_events(trace)[0].metadata


def test_ec2_stopping_mid_page_with_items_left_is_truncation():
    trace = TraceRecorder()
    client = FakeEc2Client(
        [_page(_reservation(instances=[_instance("i-1"), _instance("i-2"), _instance("i-3")]))]
    )
    assert len(_ec2(client, trace=trace, limit=2)) == 2
    assert _succeeded_events(trace)[0].metadata["truncated"] is True


def test_ec2_stopping_at_a_page_boundary_with_another_page_left_is_truncation():
    trace = TraceRecorder()
    client = FakeEc2Client(
        [
            _page(_reservation(instances=[_instance("i-1")])),
            _page(_reservation(instances=[_instance("i-2")])),
        ]
    )
    assert len(_ec2(client, trace=trace, limit=1)) == 1
    assert _succeeded_events(trace)[0].metadata["truncated"] is True


def test_ec2_collection_is_deterministic_and_leaves_the_client_untouched():
    pages = [_page(_reservation(instances=[_instance("i-2"), _instance("i-1")]))]
    first = _ec2(FakeEc2Client(pages))
    second = _ec2(FakeEc2Client(pages))
    assert [r.model_dump() for r in first] == [r.model_dump() for r in second]
    assert [r.resource_id for r in first] == ["i-2", "i-1"]  # no hidden sorting


def test_ec2_collector_names_no_mutating_ec2_operation():
    """M13-A stays on the read side of the boundary.

    The seam exposes exactly one EC2 read, so a collector that could reach
    ``stop_instances`` would be a claim about production capability this
    milestone explicitly does not make.
    """
    source = inspect.getsource(inventory_module).lower()
    assert "stop_instances" not in source
    assert "modify_instance_attribute" not in source
    assert "terminate_instances" not in source
    for name in dir(Ec2InstanceCollector):
        assert "stop" not in name.lower(), name