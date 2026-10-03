"""Workspace snapshot collection (M2C-B) with hermetic fake clients.

No network, no AWS, no credentials, no boto3. Exercises the workspace
builder end-to-end against in-memory fake clients and covers the M2C-B
model definitions (WorkspaceSnapshot, CollectionFailure).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from sws_agent.constants import (
    MAX_COST_WINDOW_DAYS,
    MAX_RESOURCES_PER_INVENTORY_REQUEST,
    CollectionFailureCategory,
    PotentialAction,
    SWSResourceType,
    TraceEventType,
    TraceStatus,
)
from sws_agent.inventory import INVENTORY_COLLECTORS
from sws_agent.models import (
    CollectionFailure,
    CostEstimate,
    ResourceRecord,
    WorkspaceSnapshot,
)
from sws_agent.trace import TraceEvent, TraceRecorder
from sws_agent.workspace import (
    _failure_from_event,
    collect_workspace,
)

BUCKET_CREATED = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
LAMBDA_ARN = "arn:aws:lambda:us-east-1:123456789012:function:fn"
PARTITION_OWNER = {"ID": "123456789012", "DisplayName": "OwnerDisplay"}
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _bucket(name: str, **overrides) -> dict:
    bucket = {"Name": name, "CreationDate": BUCKET_CREATED}
    bucket.update(overrides)
    return bucket


def _function(name: str, arn: str = "", **overrides) -> dict:
    function = {
        "FunctionName": name,
        "FunctionArn": arn or f"arn:aws:lambda:us-east-1:123456789012:function:{name}",
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
        list_error=None,
    ):
        self._buckets = list(buckets or [])
        self._owner = dict(owner or PARTITION_OWNER)
        self._locations = dict(locations or {})
        self._location_errors = set(location_errors or ())
        self._tag_sets = dict(tag_sets or {})
        self._tag_errors = set(tag_errors or ())
        self._list_error = list_error

    def list_buckets(self):
        if self._list_error is not None:
            raise self._list_error
        return {"Buckets": self._buckets, "Owner": self._owner}

    def get_bucket_location(self, Bucket):
        if Bucket in self._location_errors:
            raise ValueError(f"location denied for {Bucket}")
        return {"LocationConstraint": self._locations.get(Bucket)}

    def get_bucket_tagging(self, Bucket):
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

    def list_functions(self, Marker=None):
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
        if Resource in self._tag_errors:
            raise ValueError(f"tag access denied for {Resource}")
        return {"Tags": dict(self._tags.get(Resource) or {})}


class FakeCostClient:
    """In-memory Cost Explorer client with canned scripted responses."""

    def __init__(self, responses=None, error_at=None, error=RuntimeError("boom")):
        self._responses = [dict(r) for r in (responses or [])]
        self._error_at = set(error_at or ())
        self._error = error
        self.calls = []

    def get_cost_and_usage(self, **params):
        self.calls.append(dict(params))
        index = len(self.calls) - 1
        if index in self._error_at:
            raise self._error
        if index >= len(self._responses):
            return {"ResultsByTime": []}
        return self._responses[index]


class FakeRootClient:
    """Composite client exposing every method the collectors require.

    ``cost`` is optional: when supplied, the client also exposes
    ``get_cost_and_usage`` for the M2C-E cost collector. ``ec2`` is optional
    in the same way: without it the client has no ``describe_instances``
    method at all, which is what an account whose IAM policy omits
    ``ec2:DescribeInstances`` looks like from SWS.
    """

    def __init__(self, s3=None, lamb=None, cost=None, ec2=None):
        self._s3 = s3 or FakeS3Client(buckets=[])
        self._lamb = lamb or FakeLambdaClient(pages=[])
        self._cost = cost
        self._ec2 = ec2

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

    def get_cost_and_usage(self, **params):
        if self._cost is None:
            raise AssertionError("get_cost_and_usage called without a cost client")
        return self._cost.get_cost_and_usage(**params)

    def describe_instances(self):
        if self._ec2 is None:
            raise AssertionError("describe_instances called without an EC2 client")
        return self._ec2.describe_instances()


def _run(client: FakeRootClient, **overrides):
    defaults = {"client": client, "regions": ["us-east-1"], "now": NOW}
    defaults.update(overrides)
    return collect_workspace(**defaults)


# ---------------------------------------------------------------------------
# WorkspaceSnapshot model
# ---------------------------------------------------------------------------


def _snapshot(**overrides):
    fields = {
        "snapshot_id": "snap-1",
        "created_at": NOW,
        "regions": ["us-east-1"],
        "resource_types": [SWSResourceType.S3_BUCKET, SWSResourceType.LAMBDA_FUNCTION],
    }
    fields.update(overrides)
    return WorkspaceSnapshot(**fields)


def test_workspace_snapshot_defaults():
    snapshot = _snapshot()
    assert snapshot.collected_at is None
    assert snapshot.run_id is None
    assert snapshot.requested_limit == MAX_RESOURCES_PER_INVENTORY_REQUEST
    assert snapshot.resources == []
    assert snapshot.counts == {}
    assert snapshot.truncated is False
    assert snapshot.partial is False
    assert snapshot.failures == []


def test_workspace_snapshot_defaults_to_empty_regions_never_allowed():
    with pytest.raises(ValidationError):
        _snapshot(regions=[])


def test_workspace_snapshot_rejects_no_resource_types():
    with pytest.raises(ValidationError):
        _snapshot(resource_types=[])


def test_workspace_snapshot_rejects_blank_region():
    with pytest.raises(ValidationError):
        _snapshot(regions=["us-east-1", ""])


def test_workspace_snapshot_rejects_empty_snapshot_id():
    with pytest.raises(ValidationError):
        _snapshot(snapshot_id="")


def test_workspace_snapshot_rejects_naive_timestamps():
    with pytest.raises(ValidationError):
        _snapshot(created_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        _snapshot(collected_at=datetime(2026, 1, 1))


def test_workspace_snapshot_normalizes_resource_types_case_insensitively():
    snapshot = _snapshot(resource_types=["s3_bucket", "LAMBDA_FUNCTION"])
    assert snapshot.resource_types == [
        SWSResourceType.S3_BUCKET,
        SWSResourceType.LAMBDA_FUNCTION,
    ]


# ---------------------------------------------------------------------------
# CollectionFailure model
# ---------------------------------------------------------------------------

ALL_TYPES = [SWSResourceType.S3_BUCKET, SWSResourceType.LAMBDA_FUNCTION]


def _failure(**overrides):
    fields = {
        "resource_type": SWSResourceType.S3_BUCKET,
        "source": "s3_bucket",
        "category": CollectionFailureCategory.PRIMARY,
        "message": "inventory failed: boom",
    }
    fields.update(overrides)
    return CollectionFailure(**fields)


def test_collection_failure_defaults():
    failure = _failure()
    assert failure.fatal is False
    assert failure.resource_id is None
    assert failure.trace_event_id is None


def test_collection_failure_normalizes_enums_case_insensitively():
    failure = _failure(resource_type="S3_Bucket", category="PRIMARY")
    assert failure.resource_type is SWSResourceType.S3_BUCKET
    assert failure.category is CollectionFailureCategory.PRIMARY


def test_collection_failure_rejects_empty_message():
    with pytest.raises(ValidationError):
        _failure(message="")


def test_collection_failure_rejects_zero_trace_event_id():
    with pytest.raises(ValidationError):
        _failure(trace_event_id=0)


# ---------------------------------------------------------------------------
# collect_workspace: input validation
# ---------------------------------------------------------------------------


def test_collect_workspace_requires_non_empty_regions():
    client = FakeRootClient()
    for regions in (None, [], [""], [None]):
        with pytest.raises(ValueError, match="region"):
            _run(client, regions=regions)


def test_collect_workspace_rejects_invalid_limit():
    client = FakeRootClient()
    for limit in (0, -1, "5", True):
        with pytest.raises(ValueError):
            _run(client, limit=limit)


def test_collect_workspace_rejects_naive_now():
    client = FakeRootClient()
    with pytest.raises(ValueError, match="timezone-aware"):
        _run(client, now=datetime(2026, 1, 1))


# ---------------------------------------------------------------------------
# collect_workspace: happy path
# ---------------------------------------------------------------------------


def test_collect_workspace_empty_healthy_snapshot():
    snapshot = _run(FakeRootClient())
    assert snapshot.snapshot_id
    assert snapshot.created_at == NOW
    assert snapshot.collected_at is not None
    assert snapshot.run_id
    assert snapshot.requested_limit == MAX_RESOURCES_PER_INVENTORY_REQUEST
    assert snapshot.regions == ["us-east-1"]
    assert snapshot.resource_types == ALL_TYPES
    assert snapshot.resources == []
    assert snapshot.counts == {SWSResourceType.S3_BUCKET: 0, SWSResourceType.LAMBDA_FUNCTION: 0}
    assert snapshot.truncated is False
    assert snapshot.partial is False
    assert snapshot.failures == []


def test_collect_workspace_maps_and_canonicalizes_resources():
    s3 = FakeS3Client(
        buckets=[_bucket("my-bucket")],
        locations={"my-bucket": None},
        tag_sets={"my-bucket": [{"Key": "Owner", "Value": "alice"}]},
    )
    lamb = FakeLambdaClient(pages=[[_function("fn", LAMBDA_ARN)]])
    snapshot = _run(FakeRootClient(s3, lamb))

    assert snapshot.counts == {SWSResourceType.S3_BUCKET: 1, SWSResourceType.LAMBDA_FUNCTION: 1}
    resources = {r.resource_type: r for r in snapshot.resources}
    bucket = resources[SWSResourceType.S3_BUCKET]
    function = resources[SWSResourceType.LAMBDA_FUNCTION]
    assert bucket.arn == "arn:aws:s3:::my-bucket"
    assert bucket.account_id is None
    assert bucket.owner_tag == "alice"
    assert function.arn == LAMBDA_ARN
    assert function.account_id == "123456789012"
    assert [r.resource_type for r in snapshot.resources] == [
        SWSResourceType.LAMBDA_FUNCTION,
        SWSResourceType.S3_BUCKET,
    ]


def test_collect_workspace_partition_prefixes_s3_arns():
    s3 = FakeS3Client(buckets=[_bucket("my-bucket")], locations={})
    snapshot = _run(FakeRootClient(s3), partition="aws-cn")
    bucket = snapshot.resources[0]
    assert bucket.arn == "arn:aws-cn:s3:::my-bucket"


def test_collect_workspace_duplicate_resources_deduplicated():
    s3 = FakeS3Client(buckets=[_bucket("dup"), _bucket("dup")], locations={})
    snapshot = _run(FakeRootClient(s3))
    assert len(snapshot.resources) == 1
    assert snapshot.counts[SWSResourceType.S3_BUCKET] == 1


def test_collect_workspace_raw_payloads_unchanged():
    s3 = FakeS3Client(buckets=[_bucket("my-bucket")], locations={})
    snapshot = _run(FakeRootClient(s3))
    bucket = snapshot.resources[0]
    assert bucket.raw == {"owner_id": "123456789012", "owner_display_name": "OwnerDisplay"}


def test_collect_workspace_is_deterministic():
    def make():
        s3 = FakeS3Client(buckets=[_bucket("b1"), _bucket("b2")], locations={})
        lamb = FakeLambdaClient(pages=[[_function("f1", LAMBDA_ARN)]])
        return FakeRootClient(s3, lamb)

    first = _run(make())
    second = _run(make())
    assert first.resources == second.resources
    assert first.counts == second.counts
    assert [r.resource_id for r in first.resources] == [r.resource_id for r in second.resources]


def test_collect_workspace_custom_identity_and_timestamps():
    recorder = TraceRecorder()
    snapshot = collect_workspace(
        client=FakeRootClient(),
        regions=["us-east-1"],
        trace=recorder,
        snapshot_id="snap-custom",
        run_id="run-7",
        now=NOW,
    )
    assert snapshot.snapshot_id == "snap-custom"
    assert snapshot.run_id == "run-7"
    assert snapshot.created_at == NOW
    inventory_events = [
        e for e in recorder if e.event_type is TraceEventType.INVENTORY_QUERY
    ]
    assert inventory_events
    last = inventory_events[-1]
    assert snapshot.collected_at == datetime.fromisoformat(last.timestamp)


def test_collect_workspace_defaults_snapshot_id_when_omitted():
    client = FakeRootClient()
    a = _run(client)
    b = _run(client)
    assert a.snapshot_id
    assert b.snapshot_id
    assert a.snapshot_id != b.snapshot_id


# ---------------------------------------------------------------------------
# collect_workspace: limits and truncation
# ---------------------------------------------------------------------------


def test_collect_workspace_limit_validation_and_recorded_value():
    s3 = FakeS3Client(buckets=[_bucket(f"bucket-{i}") for i in range(200)], locations={})
    defaulted = _run(FakeRootClient(s3), limit=None)
    assert defaulted.requested_limit == MAX_RESOURCES_PER_INVENTORY_REQUEST
    assert len(defaulted.resources) == MAX_RESOURCES_PER_INVENTORY_REQUEST
    assert defaulted.truncated is True  # default cap stopped short of 200 buckets

    passed_through = _run(FakeRootClient(s3), limit=5000)
    assert passed_through.requested_limit == 5000  # positive limits pass through
    assert len(passed_through.resources) == 200

    small = _run(FakeRootClient(s3), limit=3)
    assert small.requested_limit == 3
    assert len(small.resources) == 3


def test_collect_workspace_s3_truncated_when_more_buckets_than_limit():
    s3 = FakeS3Client(buckets=[_bucket("a"), _bucket("b"), _bucket("c")], locations={})
    snapshot = _run(FakeRootClient(s3), limit=2)
    assert snapshot.truncated is True


def test_collect_workspace_s3_exact_boundary_is_not_truncated():
    s3 = FakeS3Client(buckets=[_bucket("a"), _bucket("b")], locations={})
    snapshot = _run(FakeRootClient(s3), limit=2)
    assert snapshot.truncated is False
    assert len(snapshot.resources) == 2


def test_collect_workspace_lambda_mid_page_break_is_truncated():
    lamb = FakeLambdaClient(pages=[[_function("f1"), _function("f2"), _function("f3")]])
    snapshot = _run(FakeRootClient(lamb=lamb), limit=2)
    assert snapshot.truncated is True


def test_collect_workspace_lambda_next_marker_at_stop_is_truncated():
    lamb = FakeLambdaClient(
        pages=[[_function("f1"), _function("f2")], [_function("f3"), _function("f4")]]
    )
    snapshot = _run(FakeRootClient(lamb=lamb), limit=2)
    assert snapshot.truncated is True


def test_collect_workspace_lambda_exact_boundary_is_not_truncated():
    lamb = FakeLambdaClient(pages=[[_function("f1"), _function("f2")]])
    snapshot = _run(FakeRootClient(lamb=lamb), limit=2)
    assert snapshot.truncated is False
    assert len(snapshot.resources) == 2


# ---------------------------------------------------------------------------
# collect_workspace: failures and partiality
# ---------------------------------------------------------------------------


def test_collect_workspace_partial_on_s3_primary_failure():
    s3 = FakeS3Client(buckets=[_bucket("x")], list_error=RuntimeError("boom"))
    snapshot = _run(FakeRootClient(s3))
    assert snapshot.partial is True
    assert snapshot.resources == []
    assert snapshot.counts[SWSResourceType.S3_BUCKET] == 0
    assert snapshot.resource_types == ALL_TYPES  # intent recorded even on failure
    assert len(snapshot.failures) == 1
    failure = snapshot.failures[0]
    assert failure.resource_type is SWSResourceType.S3_BUCKET
    assert failure.source == "s3_bucket"
    assert failure.category is CollectionFailureCategory.PRIMARY
    assert failure.fatal is True
    assert failure.resource_id is None
    assert failure.trace_event_id is not None


def test_collect_workspace_partial_on_lambda_primary_failure():
    lamb = FakeLambdaClient(pages=[[_function("f1")]], list_error=RuntimeError("boom"))
    s3 = FakeS3Client(buckets=[_bucket("b")], locations={})
    snapshot = _run(FakeRootClient(s3, lamb))
    assert snapshot.partial is True
    assert snapshot.counts[SWSResourceType.S3_BUCKET] == 1
    assert snapshot.counts[SWSResourceType.LAMBDA_FUNCTION] == 0
    assert len(snapshot.failures) == 1
    assert snapshot.failures[0].resource_type is SWSResourceType.LAMBDA_FUNCTION
    assert snapshot.failures[0].category is CollectionFailureCategory.PRIMARY


def test_collect_workspace_enrichment_failure_makes_snapshot_partial():
    s3 = FakeS3Client(
        buckets=[_bucket("kept")],
        locations={},
        tag_errors={"kept"},
    )
    recorder = TraceRecorder()
    snapshot = collect_workspace(
        client=FakeRootClient(s3),
        regions=["us-east-1"],
        trace=recorder,
        now=NOW,
    )
    assert snapshot.partial is True  # any run-scoped FAILED event, even non-fatal
    assert snapshot.truncated is False
    assert len(snapshot.resources) == 1  # record preserved despite tag failure
    assert snapshot.resources[0].owner_tag is None
    assert len(snapshot.failures) == 1
    failure = snapshot.failures[0]
    assert failure.resource_type is SWSResourceType.S3_BUCKET
    assert failure.category is CollectionFailureCategory.ENRICHMENT
    assert failure.fatal is False
    assert failure.resource_id == "kept"
    failed_events = [
        e for e in recorder
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]
    assert len(failed_events) == 1
    assert failure.trace_event_id == failed_events[0].event_id  # 1:1 linkage through trace


def test_collect_workspace_parse_failures_mapped_but_skipped():
    bad_no_arn = _function("noarn")
    del bad_no_arn["FunctionArn"]
    lamb = FakeLambdaClient(
        pages=[
            [
                _function("good", LAMBDA_ARN),
                _function("malformed", "not-an-arn"),
                bad_no_arn,
            ]
        ]
    )
    snapshot = _run(FakeRootClient(lamb=lamb))
    assert snapshot.partial is True  # parse skips are run-scoped failures too
    assert len(snapshot.resources) == 1
    assert snapshot.resources[0].resource_type is SWSResourceType.LAMBDA_FUNCTION
    assert snapshot.counts[SWSResourceType.LAMBDA_FUNCTION] == 1
    assert len(snapshot.failures) == 2
    categories = {f.category for f in snapshot.failures}
    assert categories == {CollectionFailureCategory.PARSE}
    assert all(not f.fatal for f in snapshot.failures)


def test_partial_reflects_any_run_scoped_failure_regardless_of_fatal():
    """Regression: partial is driven by FAILED events, not by fatal flag."""
    primary = FakeS3Client(buckets=[_bucket("x")], list_error=RuntimeError("boom"))
    assert _run(FakeRootClient(primary)).partial is True

    enrichment = FakeS3Client(buckets=[_bucket("kept")], locations={}, tag_errors={"kept"})
    snapshot = _run(FakeRootClient(enrichment))
    assert snapshot.partial is True
    assert snapshot.failures[0].fatal is False

    parse = FakeLambdaClient(pages=[[_function("malformed", "not-an-arn")]])
    snapshot = _run(FakeRootClient(lamb=parse))
    assert snapshot.partial is True
    assert snapshot.failures[0].fatal is False

    assert _run(FakeRootClient()).partial is False  # empty healthy collection


def test_collect_workspace_run_scoping_ignores_pre_seeded_events():
    recorder = TraceRecorder()
    recorder.fail(
        TraceEventType.INVENTORY_QUERY,
        "stale s3 failure",
        metadata={"resource_type": "s3_bucket", "category": "primary"},
    )
    recorder.succeed(
        TraceEventType.INVENTORY_QUERY,
        "stale s3 success",
        metadata={"resource_type": "s3_bucket", "count": 5, "truncated": True},
    )
    snapshot = collect_workspace(
        client=FakeRootClient(),
        regions=["us-east-1"],
        trace=recorder,
        now=NOW,
    )
    assert snapshot.partial is False
    assert snapshot.truncated is False
    assert snapshot.failures == []
    # 2 seeds + 4 fresh events (RUNNING + terminal per collector, S3 then Lambda)
    assert len(list(recorder)) == 6


def test_collect_workspace_uses_fresh_recorder_when_none_provided():
    snapshot = _run(FakeRootClient())
    assert snapshot.collected_at is not None
    assert snapshot.failures == []


# ---------------------------------------------------------------------------
# Defensive category fallback (approved Option A fallback rule)
# ---------------------------------------------------------------------------


def test_failure_category_fallback_enrichment_when_succeeded_present():
    event = TraceEvent(
        event_id=9,
        event_type=TraceEventType.INVENTORY_QUERY,
        status=TraceStatus.FAILED,
        message="failed to read tags",
        timestamp="2026-01-01T00:00:00+00:00",
        metadata={"resource_type": "s3_bucket", "resource_id": "b1"},
    )
    failure = _failure_from_event(
        event, succeeded_types={SWSResourceType.S3_BUCKET}
    )
    assert failure is not None
    assert failure.category is CollectionFailureCategory.ENRICHMENT
    assert failure.fatal is False
    assert failure.trace_event_id == 9
    assert failure.resource_id == "b1"


def test_failure_category_fallback_primary_when_no_success():
    event = TraceEvent(
        event_id=10,
        event_type=TraceEventType.INVENTORY_QUERY,
        status=TraceStatus.FAILED,
        message="inventory failed",
        timestamp="2026-01-01T00:00:00+00:00",
        metadata={"resource_type": "s3_bucket"},
    )
    failure = _failure_from_event(event, succeeded_types=set())
    assert failure is not None
    assert failure.category is CollectionFailureCategory.PRIMARY
    assert failure.fatal is True


def test_failure_without_resource_type_is_not_attributed():
    event = TraceEvent(
        event_id=11,
        event_type=TraceEventType.INVENTORY_QUERY,
        status=TraceStatus.FAILED,
        message="unattributable",
        timestamp="2026-01-01T00:00:00+00:00",
        metadata={},
    )
    assert _failure_from_event(event, succeeded_types=set()) is None


# ---------------------------------------------------------------------------
# collect_workspace: M2C-E cost integration
# ---------------------------------------------------------------------------

COST_END = date(2026, 3, 31)


def _total_response(amount: str) -> dict:
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
                "Total": {
                    "UnblendedCost": {"Amount": amount, "Unit": "USD"}
                },
            }
        ]
    }


def test_collect_workspace_without_cost_leaves_cost_empty_and_calls_no_cost_api():
    snapshot = _run(FakeRootClient())
    assert snapshot.cost == []
    # default path never touches get_cost_and_usage
    client = FakeRootClient()
    _run(client)
    assert client._cost is None  # nothing requested get_cost_and_usage


def test_collect_workspace_requires_cost_end_date_when_collecting_cost():
    client = FakeRootClient(cost=FakeCostClient(responses=[_total_response("1.0")]))
    with pytest.raises(ValueError, match="cost_end_date"):
        collect_workspace(
            client=client,
            regions=["us-east-1"],
            now=NOW,
            collect_cost=True,
        )


def test_collect_workspace_collects_cost_estimates_on_snapshot():
    cost_client = FakeCostClient(responses=[_total_response("12.50")])
    client = FakeRootClient(cost=cost_client)
    snapshot = collect_workspace(
        client=client,
        regions=["us-east-1"],
        now=NOW,
        collect_cost=True,
        cost_end_date=COST_END,
    )
    assert isinstance(snapshot.cost, list)
    assert len(snapshot.cost) == 1
    assert isinstance(snapshot.cost[0], CostEstimate)
    assert snapshot.cost[0].line_item == "total"
    assert snapshot.cost[0].amount_usd == 12.5
    assert cost_client.calls and "TimePeriod" in cost_client.calls[0]


def test_collect_workspace_cost_never_enters_resources_or_counts():
    s3 = FakeS3Client(buckets=[_bucket("my-bucket")], locations={})
    cost_client = FakeCostClient(responses=[_total_response("12.50")])
    snapshot = collect_workspace(
        client=FakeRootClient(s3, cost=cost_client),
        regions=["us-east-1"],
        now=NOW,
        collect_cost=True,
        cost_end_date=COST_END,
    )
    assert {r.resource_type for r in snapshot.resources} == {
        SWSResourceType.S3_BUCKET,
    }
    assert SWSResourceType.COST_DATA not in {
        r.resource_type for r in snapshot.resources
    }
    assert set(snapshot.counts) == {
        SWSResourceType.S3_BUCKET,
        SWSResourceType.LAMBDA_FUNCTION,
    }
    assert SWSResourceType.COST_DATA not in snapshot.counts
    assert SWSResourceType.COST_DATA not in snapshot.resource_types
    assert snapshot.counts[SWSResourceType.S3_BUCKET] == 1
    assert len(snapshot.cost) == 1


def test_collect_workspace_cost_primary_failure_marks_partial():
    cost_client = FakeCostClient(
        responses=[_total_response("1.0")],
        error_at={0},
    )
    client = FakeRootClient(cost=cost_client)
    snapshot = collect_workspace(
        client=client,
        regions=["us-east-1"],
        now=NOW,
        collect_cost=True,
        cost_end_date=COST_END,
    )
    assert snapshot.partial is True
    assert snapshot.cost == []
    assert any(
        f.resource_type is SWSResourceType.COST_DATA
        for f in snapshot.failures
    )
    cost_failure = next(
        f for f in snapshot.failures if f.resource_type is SWSResourceType.COST_DATA
    )
    assert cost_failure.category is CollectionFailureCategory.PRIMARY
    assert cost_failure.fatal is True


def test_collect_workspace_cost_grouped_failure_returns_totals_and_partial():
    response = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
                "Groups": [
                    {
                        "Keys": ["AmazonS3"],
                        "Metrics": {"UnblendedCost": {"Amount": "5.0", "Unit": "USD"}},
                    }
                ],
            }
        ]
    }
    cost_client = FakeCostClient(
        responses=[_total_response("9.0"), response],
        error_at={1},
    )
    client = FakeRootClient(cost=cost_client)
    snapshot = collect_workspace(
        client=client,
        regions=["us-east-1"],
        now=NOW,
        collect_cost=True,
        cost_end_date=COST_END,
        cost_group_by=["service"],
    )
    assert snapshot.partial is True  # grouped failure is a run-scoped failure
    assert [e.line_item for e in snapshot.cost] == ["total"]
    assert snapshot.cost[0].amount_usd == 9.0
    cost_failure = next(
        f for f in snapshot.failures if f.resource_type is SWSResourceType.COST_DATA
    )
    assert cost_failure.category is CollectionFailureCategory.ENRICHMENT
    assert cost_failure.fatal is False


def test_collect_workspace_cost_validates_bounds():
    client = FakeRootClient(cost=FakeCostClient(responses=[_total_response("1.0")]))
    for window_days in (0, MAX_COST_WINDOW_DAYS + 1, -3):
        with pytest.raises(ValueError):
            collect_workspace(
                client=client,
                regions=["us-east-1"],
                now=NOW,
                collect_cost=True,
                cost_end_date=COST_END,
                cost_window_days=window_days,
            )
    with pytest.raises(ValueError):
        collect_workspace(
            client=client,
            regions=["us-east-1"],
            now=NOW,
            collect_cost=True,
            cost_end_date=COST_END,
            cost_group_by=["unknown_key"],
        )


def test_workspace_snapshot_cost_defaults_to_empty():
    snapshot = _snapshot()
    assert snapshot.cost == []


def test_cost_presence_does_not_change_policy_outcomes():
    """Policy evaluation is about resources, never about cost figures."""
    from sws_agent.policy import evaluate_workspace

    snapshot = _snapshot(
        resources=[
            ResourceRecord(
                resource_id="bucket-a",
                resource_type=SWSResourceType.S3_BUCKET,
                owner_tag=None,
            )
        ],
        cost=[CostEstimate(line_item="total", amount_usd=999.0, basis="b")],
    )
    decisions = evaluate_workspace(snapshot)
    assert [d.rule for d in decisions] == ["missing_owner_tag"]


# ---------------------------------------------------------------------------
# EC2 opt-in collection (M13-A)
# ---------------------------------------------------------------------------

EC2_ACCOUNT = "123456789012"
EC2_LAUNCH_TIME = datetime(2026, 2, 1, 9, 30, tzinfo=timezone.utc)


class FakeEc2Paginator:
    def __init__(self, pages):
        self._pages = list(pages)

    def paginate(self, **kwargs):
        return iter(self._pages)


class FakeEc2Client:
    """Canned ``ec2:DescribeInstances`` seam for workspace-level runs."""

    def __init__(self, pages=None, *, error=None):
        self._pages = list(pages or [])
        self._error = error
        self.calls = 0

    def describe_instances(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return FakeEc2Paginator(self._pages)


def _ec2_page(*instances):
    return {
        "Reservations": [
            {"OwnerId": EC2_ACCOUNT, "Instances": list(instances)}
        ]
    }


def _ec2_instance(instance_id, state="running", **extra):
    payload = {"InstanceId": instance_id, "State": {"Code": 16, "Name": state}}
    payload.update(extra)
    return payload


def _ec2_run(**overrides):
    defaults = {
        "client": FakeRootClient(
            ec2=FakeEc2Client(
                [_ec2_page(_ec2_instance("i-running"), _ec2_instance("i-stopped", "stopped"))]
            )
        ),
        "regions": ["us-east-1"],
        "now": NOW,
        "collect_ec2": True,
    }
    defaults.update(overrides)
    return collect_workspace(**defaults)


def test_default_run_does_not_read_ec2_or_claim_it_as_intent():
    """A run must never list a type whose collector it did not invoke.

    ``resource_types`` records intent. Listing EC2 in a run that never read it
    would report ``counts['ec2_instance'] == 0``, which is indistinguishable
    from "this account has no instances" -- a fabricated finding.
    """
    ec2 = FakeEc2Client([_ec2_page(_ec2_instance("i-1"))])
    snapshot = _run(FakeRootClient(ec2=ec2))
    assert ec2.calls == 0
    assert snapshot.resource_types == [
        SWSResourceType.S3_BUCKET,
        SWSResourceType.LAMBDA_FUNCTION,
    ]
    assert SWSResourceType.EC2_INSTANCE not in snapshot.counts


def test_default_run_stays_clean_against_a_client_with_no_ec2_access():
    """EC2 being opt-in is what keeps a non-EC2 workspace out of ``partial``."""
    snapshot = _run(FakeRootClient())
    assert snapshot.partial is False
    assert snapshot.failures == []
    assert snapshot.collected_at is not None


def test_opted_in_run_records_the_read_in_both_data_and_intent():
    ec2 = FakeEc2Client([_ec2_page(_ec2_instance("i-running"), _ec2_instance("i-stopped", "stopped"))])
    snapshot = _run(FakeRootClient(ec2=ec2), collect_ec2=True)
    assert ec2.calls == 1
    assert snapshot.resource_types == [
        SWSResourceType.S3_BUCKET,
        SWSResourceType.LAMBDA_FUNCTION,
        SWSResourceType.EC2_INSTANCE,
    ]
    assert snapshot.counts[SWSResourceType.EC2_INSTANCE] == 2
    assert snapshot.partial is False


def test_opted_in_run_carries_the_observed_instance_state_into_the_snapshot():
    snapshot = _ec2_run()
    records = {r.resource_id: r for r in snapshot.resources}
    assert records["i-running"].state == "running"
    assert records["i-stopped"].state == "stopped"
    assert records["i-running"].resource_type is SWSResourceType.EC2_INSTANCE
    assert records["i-running"].region == "us-east-1"
    assert records["i-running"].account_id == EC2_ACCOUNT
    assert records["i-running"].owner_tag is None


def test_opted_in_snapshot_drives_the_stop_recommendation_end_to_end():
    """The M13-A path: read an instance, then recommend a stop on it.

    This is the first time an EC2 resource can reach a decision at all. It
    proves inventory and policy are wired to each other, not merely that each
    half works alone -- and that what comes out is still a recommendation.
    """
    from sws_agent.policy import evaluate_workspace

    snapshot = _ec2_run()
    decisions = {d.resource_id: d for d in evaluate_workspace(snapshot)}
    assert decisions["i-running"].recommended_action is PotentialAction.STOP_RESOURCE
    assert decisions["i-running"].needs_approval is True
    assert decisions["i-stopped"].recommended_action is PotentialAction.LEAVE
    assert all(d.snapshot_id == snapshot.snapshot_id for d in decisions.values())


def test_ec2_read_failure_makes_the_snapshot_partial_with_a_primary_failure():
    """A failed opt-in read is surfaced, never swallowed into an empty result."""
    snapshot = _run(
        FakeRootClient(ec2=FakeEc2Client(error=RuntimeError("denied"))),
        collect_ec2=True,
    )
    assert snapshot.partial is True
    ec2_failures = [
        f for f in snapshot.failures if f.resource_type is SWSResourceType.EC2_INSTANCE
    ]
    assert len(ec2_failures) == 1
    assert ec2_failures[0].fatal is True
    assert ec2_failures[0].category is CollectionFailureCategory.PRIMARY
    assert snapshot.counts[SWSResourceType.EC2_INSTANCE] == 0


def test_missing_ec2_permission_is_reported_not_treated_as_zero_instances():
    """No ``describe_instances`` at all is a failure, not an empty account.

    Without this distinction an account lacking the permission would produce a
    confident "0 instances" and a clean snapshot, which is the one reading
    that would later justify stopping something.
    """
    snapshot = _run(FakeRootClient(), collect_ec2=True)
    assert snapshot.partial is True
    assert [f.resource_type for f in snapshot.failures] == [
        SWSResourceType.EC2_INSTANCE
    ]
    assert snapshot.counts[SWSResourceType.EC2_INSTANCE] == 0


def test_account_with_no_instances_is_a_clean_zero_not_a_failure():
    ec2 = FakeEc2Client([{"Reservations": []}])
    snapshot = _run(FakeRootClient(ec2=ec2), collect_ec2=True)
    assert snapshot.partial is False
    assert snapshot.failures == []
    assert snapshot.counts[SWSResourceType.EC2_INSTANCE] == 0


def test_ec2_truncation_surfaces_on_the_snapshot():
    pages = [
        _ec2_page(_ec2_instance("i-1")),
        _ec2_page(_ec2_instance("i-2")),
    ]
    snapshot = _run(FakeRootClient(ec2=FakeEc2Client(pages)), collect_ec2=True, limit=1)
    assert snapshot.truncated is True
    assert snapshot.counts[SWSResourceType.EC2_INSTANCE] == 1


def test_default_resource_types_are_explicit_and_ec2_is_not_one_of_them():
    """Registering a collector must not silently widen every existing run."""
    from sws_agent.workspace import DEFAULT_RESOURCE_TYPES

    assert DEFAULT_RESOURCE_TYPES == (
        SWSResourceType.S3_BUCKET,
        SWSResourceType.LAMBDA_FUNCTION,
    )
    assert SWSResourceType.EC2_INSTANCE in INVENTORY_COLLECTORS


def test_multi_region_run_is_fine_without_ec2():
    """The single-region restriction belongs to EC2, not to the workspace."""
    snapshot = _run(FakeRootClient(), regions=["us-east-1", "eu-west-1"])
    assert snapshot.partial is False
    assert snapshot.regions == ["us-east-1", "eu-west-1"]


def test_multi_region_ec2_run_raises_before_any_collector_runs():
    """Refusing beats mislabeling, and it must cost no wasted collection.

    The client seam reads one configured region, so a two-region EC2 request
    could only duplicate one region's instances under invented locations. The
    check runs before collection so a rejected call cannot leave a partial
    trace that looks like a failed run.
    """
    ec2 = FakeEc2Client([_ec2_page(_ec2_instance("i-1"))])
    s3 = FakeS3Client(buckets=[{"Name": "bucket-a"}])
    client = FakeRootClient(s3=s3, ec2=ec2)
    trace = TraceRecorder()
    with pytest.raises(ValueError, match="exactly one region"):
        collect_workspace(
            client=client,
            regions=["us-east-1", "eu-west-1"],
            now=NOW,
            trace=trace,
            collect_ec2=True,
        )
    assert ec2.calls == 0
    assert len(trace) == 0
    assert trace.to_dicts() == []