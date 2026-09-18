"""Canonical identity and normalization (M2C-A) with hermetic fixtures.

No network, no AWS, no credentials, no boto3. Exercises the pure
canonicalization layer on hand-built ResourceRecord instances and verifies
backward compatibility with M2B-era constructions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sws_agent.canonical import canonicalize_resources
from sws_agent.constants import SWSResourceType
from sws_agent.models import ResourceRecord

LAMBDA_ARN = "arn:aws:lambda:us-east-1:123456789012:function:my-func"
S3_CANONICAL_USER_ID = (
    "79a59df900b949e55d96a1e698fbacedfd6e09d98eacf8f8d5218e7cd47ef2be"
)


def _record(
    resource_id: str, resource_type: SWSResourceType, **overrides
) -> ResourceRecord:
    fields = {"resource_id": resource_id, "resource_type": resource_type}
    fields.update(overrides)
    return ResourceRecord(**fields)


def test_lambda_arn_copied_and_account_extracted():
    record = _record(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, name="my-func")
    out = canonicalize_resources([record])
    assert len(out) == 1
    assert out[0].arn == LAMBDA_ARN
    assert out[0].account_id == "123456789012"


def test_lambda_malformed_arn_keeps_arn_but_no_account():
    record = _record("not-an-arn", SWSResourceType.LAMBDA_FUNCTION)
    out = canonicalize_resources([record])
    assert out[0].arn == "not-an-arn"
    assert out[0].account_id is None


def test_lambda_arn_missing_account_segment_has_no_account():
    record = _record(
        "arn:aws:lambda:us-west-2:function:lonely",
        SWSResourceType.LAMBDA_FUNCTION,
    )
    out = canonicalize_resources([record])
    assert out[0].account_id is None


def test_lambda_arn_non_digit_account_has_no_account():
    record = _record(
        "arn:aws:lambda:us-gov-west-1:not-12-digits:function:my-func",
        SWSResourceType.LAMBDA_FUNCTION,
    )
    out = canonicalize_resources([record])
    assert out[0].account_id is None


def test_lambda_arn_from_other_service_is_not_parsed():
    record = _record("arn:aws:s3:::my-bucket", SWSResourceType.LAMBDA_FUNCTION)
    out = canonicalize_resources([record])
    assert out[0].arn == "arn:aws:s3:::my-bucket"
    assert out[0].account_id is None


def test_lambda_arn_ignores_partition():
    record = _record(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION)
    out = canonicalize_resources([record], partition="aws-cn")
    assert out[0].arn == LAMBDA_ARN
    assert out[0].account_id == "123456789012"


def test_s3_arn_derived_from_bucket_name():
    record = _record("my-bucket", SWSResourceType.S3_BUCKET)
    out = canonicalize_resources([record])
    assert out[0].arn == "arn:aws:s3:::my-bucket"
    assert out[0].account_id is None


def test_s3_arn_respects_partition():
    record = _record("my-bucket", SWSResourceType.S3_BUCKET)
    out = canonicalize_resources([record], partition="aws-cn")
    assert out[0].arn == "arn:aws-cn:s3:::my-bucket"


def test_s3_canonical_user_id_never_becomes_account_id():
    raw = {"owner_id": S3_CANONICAL_USER_ID, "owner_display_name": "OwnerDisplay"}
    record = _record("my-bucket", SWSResourceType.S3_BUCKET, raw=raw)
    out = canonicalize_resources([record])
    assert out[0].arn == "arn:aws:s3:::my-bucket"
    assert out[0].account_id is None
    assert out[0].raw == raw


def test_identity_fields_default_to_none():
    record = ResourceRecord(resource_id="rn-1", resource_type="S3_Bucket")
    assert record.arn is None
    assert record.account_id is None


def test_unknown_resource_type_is_not_fabricated():
    record = _record("i-123", SWSResourceType.EC2_INSTANCE)
    out = canonicalize_resources([record])
    assert out[0].arn is None
    assert out[0].account_id is None


def test_account_id_rejects_non_12_digit_value():
    with pytest.raises(ValidationError):
        ResourceRecord(
            resource_id=LAMBDA_ARN,
            resource_type=SWSResourceType.LAMBDA_FUNCTION,
            account_id="12345678901",
        )


def test_account_id_accepts_12_digit_value():
    record = ResourceRecord(
        resource_id=LAMBDA_ARN,
        resource_type=SWSResourceType.LAMBDA_FUNCTION,
        account_id="012345678901",
    )
    assert record.account_id == "012345678901"


def test_timezone_aware_created_at_normalized_to_utc():
    offset_time = datetime(2026, 1, 15, 12, 0, tzinfo=timezone(timedelta(hours=5)))
    record = _record("my-bucket", SWSResourceType.S3_BUCKET, created_at=offset_time)
    out = canonicalize_resources([record])
    assert out[0].created_at == offset_time.astimezone(timezone.utc)
    assert out[0].created_at.tzinfo == timezone.utc


def test_naive_created_at_raises_value_error():
    record = _record(
        "my-bucket",
        SWSResourceType.S3_BUCKET,
        created_at=datetime(2026, 1, 15, 12, 0),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        canonicalize_resources([record])


def test_null_created_at_preserved():
    record = _record(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, created_at=None)
    out = canonicalize_resources([record])
    assert out[0].created_at is None


def test_duplicates_deduplicate_first_wins():
    first = _record("my-bucket", SWSResourceType.S3_BUCKET, name="first")
    second = _record("my-bucket", SWSResourceType.S3_BUCKET, name="second")
    out = canonicalize_resources([first, second])
    assert len(out) == 1
    assert out[0].name == "first"


def test_ordering_is_deterministic():
    records = [
        _record(
            "arn:aws:lambda:us-east-1:123456789012:function:fn",
            SWSResourceType.LAMBDA_FUNCTION,
            name="fn",
        ),
        _record("b1", SWSResourceType.S3_BUCKET),
        _record("a1", SWSResourceType.S3_BUCKET),
    ]
    out = canonicalize_resources(records)
    assert [r.resource_id for r in out] == [
        "arn:aws:lambda:us-east-1:123456789012:function:fn",
        "a1",
        "b1",
    ]


def test_canonicalization_is_idempotent():
    records = [
        _record(
            LAMBDA_ARN,
            SWSResourceType.LAMBDA_FUNCTION,
            created_at=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        ),
        _record(
            "b1",
            SWSResourceType.S3_BUCKET,
            created_at=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        ),
    ]
    once = canonicalize_resources(records)
    twice = canonicalize_resources(once)
    assert once == twice


def test_input_records_are_not_mutated():
    record = _record(
        "my-bucket",
        SWSResourceType.S3_BUCKET,
        created_at=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
    )
    canonicalize_resources([record])
    assert record.arn is None
    assert record.account_id is None
    assert record.created_at == datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def test_raw_fields_unchanged():
    raw = {"runtime": "python3.12", "memory_mb": 128}
    record = _record(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, raw=raw)
    out = canonicalize_resources([record])
    assert out[0].raw == raw


def test_empty_input_yields_empty_output():
    assert canonicalize_resources([]) == []


def test_partition_must_be_non_empty():
    with pytest.raises(ValueError, match="partition"):
        canonicalize_resources([], partition="")


def test_resource_record_construction_is_backward_compatible():
    record = ResourceRecord(resource_id="rn-1", resource_type="S3_Bucket")
    assert record.resource_id == "rn-1"
    assert record.resource_type is SWSResourceType.S3_BUCKET
    assert record.name is None
    assert record.region is None
    assert record.owner_tag is None
    assert record.created_at is None
    assert record.metrics == {}
    assert record.raw == {}
    assert record.arn is None
    assert record.account_id is None