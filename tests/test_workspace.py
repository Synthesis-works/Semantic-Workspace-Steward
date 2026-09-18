"""Workspace snapshot collection (M2C-B) with hermetic fake clients.

No network, no AWS, no credentials, no boto3. Exercises the workspace
builder end-to-end against in-memory fake clients and covers the M2C-B
model definitions (WorkspaceSnapshot, CollectionFailure).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sws_agent.constants import (
    MAX_RESOURCES_PER_INVENTORY_REQUEST,
    CollectionFailureCategory,
    SWSResourceType,
    TraceEventType,
    TraceStatus,
)
from sws_agent.models import CollectionFailure, WorkspaceSnapshot
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

    def list_tags(self, Target):
        if Target in self._tag_errors:
            raise ValueError(f"tag access denied for {Target}")
        return {"Tags": dict(self._tags.get(Target) or {})}


class FakeRootClient:
    """Composite client exposing every method the collectors require."""

    def __init__(self, s3=None, lamb=None):
        self._s3 = s3 or FakeS3Client(buckets=[])
        self._lamb = lamb or FakeLambdaClient(pages=[])

    def list_buckets(self):
        return self._s3.list_buckets()

    def get_bucket_location(self, Bucket):
        return self._s3.get_bucket_location(Bucket)

    def get_bucket_tagging(self, Bucket):
        return self._s3.get_bucket_tagging(Bucket)

    def list_functions(self, Marker=None):
        return self._lamb.list_functions(Marker)

    def list_tags(self, Target):
        return self._lamb.list_tags(Target)


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
    assert snapshot.run_id is None
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