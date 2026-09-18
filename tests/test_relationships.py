"""Deterministic evidence takes precedence over semantic inference.

Covers merge_relationships (deterministic-over-inferred precedence) and
derive_relationships (deterministic derivation within a snapshot).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sws_agent.constants import (
    ClaimKind,
    EvidenceBasis,
    RelationshipType,
    SWSResourceType,
)
from sws_agent.models import (
    ResourceRecord,
    ResourceRelationship,
    WorkspaceSnapshot,
)
from sws_agent.relationships import (
    WorkspaceSnapshotTooLargeError,
    derive_relationships,
    merge_relationships,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _relationship(source, target, kind, basis, confidence=1.0):
    return ResourceRelationship(
        source_id=source,
        target_id=target,
        relationship_type=kind,
        basis=basis,
        confidence=confidence,
    )


def test_inferred_relationship_never_overrides_deterministic():
    deterministic = _relationship(
        "a", "b", RelationshipType.SAME_ACCOUNT, EvidenceBasis.DETERMINISTIC
    )
    inferred = _relationship(
        "a", "b", RelationshipType.SAME_ACCOUNT, EvidenceBasis.INFERRED
    )
    merged = merge_relationships(deterministic=[deterministic], inferred=[inferred])
    assert len(merged) == 1
    assert merged[0].basis is EvidenceBasis.DETERMINISTIC


def test_inferred_relationship_added_when_no_deterministic_exists():
    inferred = _relationship(
        "a", "b", RelationshipType.SAME_REGION, EvidenceBasis.INFERRED
    )
    merged = merge_relationships(deterministic=[], inferred=[inferred])
    assert len(merged) == 1
    assert merged[0].basis is EvidenceBasis.INFERRED


def test_stable_sorted_output():
    deterministic = _relationship(
        "a", "b", RelationshipType.SAME_ACCOUNT, EvidenceBasis.DETERMINISTIC
    )
    inferred = _relationship(
        "a", "b", RelationshipType.SAME_REGION, EvidenceBasis.INFERRED
    )
    merged = merge_relationships(deterministic=[deterministic], inferred=[inferred])
    assert [rel.relationship_type for rel in merged] == [
        RelationshipType.SAME_ACCOUNT,
        RelationshipType.SAME_REGION,
    ]


# ---------------------------------------------------------------------------
# derive_relationships: hermetic fixtures
# ---------------------------------------------------------------------------


def _resource(resource_id: str, resource_type=SWSResourceType.S3_BUCKET, **overrides):
    fields = {"resource_id": resource_id, "resource_type": resource_type}
    fields.update(overrides)
    return ResourceRecord(**fields)


def _snapshot(*resources, partial=False, truncated=False):
    return WorkspaceSnapshot(
        snapshot_id="snap-1",
        created_at=NOW,
        regions=["us-east-1"],
        resource_types=[
            SWSResourceType.S3_BUCKET,
            SWSResourceType.LAMBDA_FUNCTION,
        ],
        resources=list(resources),
        partial=partial,
        truncated=truncated,
    )


LAMBDA_ARN = "arn:aws:lambda:us-east-1:123456789012:function:fn"


def test_same_account_derived_when_both_known_and_equal():
    snapshot = _snapshot(
        _resource(
            LAMBDA_ARN,
            SWSResourceType.LAMBDA_FUNCTION,
            account_id="123456789012",
        ),
        _resource("b", SWSResourceType.S3_BUCKET, account_id="123456789012"),
    )
    relationships = derive_relationships(snapshot)
    types = [rel.relationship_type for rel in relationships]
    assert types == [RelationshipType.SAME_ACCOUNT]
    relationship = relationships[0]
    assert relationship.source_id == LAMBDA_ARN  # canonical index order: lambda first
    assert relationship.target_id == "b"
    assert relationship.basis is EvidenceBasis.DETERMINISTIC
    assert relationship.claim_kind is ClaimKind.DERIVED
    assert relationship.confidence == 1.0
    assert relationship.evidence == [
        {"attribute": "account_id", "value": "123456789012"}
    ]


def test_same_account_not_derived_when_unequal():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, account_id="123456789012"),
        _resource("b", SWSResourceType.S3_BUCKET, account_id="111122223333"),
    )
    assert derive_relationships(snapshot) == []


def test_same_account_not_derived_when_unknown():
    for kwargs in (
        {"account_id": None, "account_id_b": "123456789012"},
        {"account_id": "123456789012", "account_id_b": None},
        {"account_id": None, "account_id_b": None},
    ):
        snapshot = _snapshot(
            _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, account_id=kwargs["account_id"]),
            _resource("b", SWSResourceType.S3_BUCKET, account_id=kwargs["account_id_b"]),
        )
        assert derive_relationships(snapshot) == []


def test_same_region_derived_when_both_known_and_equal():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, region="us-east-1"),
        _resource("b", SWSResourceType.S3_BUCKET, region="us-east-1"),
    )
    relationships = derive_relationships(snapshot)
    assert len(relationships) == 1
    assert relationships[0].relationship_type is RelationshipType.SAME_REGION
    assert relationships[0].evidence == [{"attribute": "region", "value": "us-east-1"}]


def test_same_region_not_derived_when_unequal():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, region="us-east-1"),
        _resource("b", SWSResourceType.S3_BUCKET, region="eu-west-1"),
    )
    assert derive_relationships(snapshot) == []


def test_same_region_not_derived_when_unknown():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, region=None),
        _resource("b", SWSResourceType.S3_BUCKET, region="us-east-1"),
    )
    assert derive_relationships(snapshot) == []


def test_same_owner_tag_derived_when_both_known_and_equal():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, owner_tag="alice"),
        _resource("b", SWSResourceType.S3_BUCKET, owner_tag="alice"),
    )
    relationships = derive_relationships(snapshot)
    assert len(relationships) == 1
    assert relationships[0].relationship_type is RelationshipType.SAME_OWNER_TAG
    assert relationships[0].evidence == [
        {"attribute": "owner_tag", "value": "alice"}
    ]


def test_same_owner_tag_not_derived_when_unequal():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, owner_tag="alice"),
        _resource("b", SWSResourceType.S3_BUCKET, owner_tag="bob"),
    )
    assert derive_relationships(snapshot) == []


def test_same_owner_tag_not_derived_when_unknown():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, owner_tag=None),
        _resource("b", SWSResourceType.S3_BUCKET, owner_tag="alice"),
    )
    assert derive_relationships(snapshot) == []


def test_multiple_relationship_types_for_one_pair():
    snapshot = _snapshot(
        _resource(
            LAMBDA_ARN,
            SWSResourceType.LAMBDA_FUNCTION,
            account_id="123456789012",
            region="us-east-1",
            owner_tag="alice",
        ),
        _resource(
            "b",
            SWSResourceType.S3_BUCKET,
            account_id="123456789012",
            region="us-east-1",
            owner_tag="alice",
        ),
    )
    types = [rel.relationship_type for rel in derive_relationships(snapshot)]
    assert types == [
        RelationshipType.SAME_ACCOUNT,
        RelationshipType.SAME_OWNER_TAG,
        RelationshipType.SAME_REGION,
    ]


def test_no_self_pairs():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, account_id="123456789012")
    )
    assert derive_relationships(snapshot) == []


def test_canonical_pair_direction_and_stable_ordering():
    snapshot = _snapshot(
        _resource("lambda-a", SWSResourceType.LAMBDA_FUNCTION, region="us-east-1"),
        _resource("bucket-a", SWSResourceType.S3_BUCKET, region="us-east-1"),
        _resource("bucket-b", SWSResourceType.S3_BUCKET, region="us-east-1"),
    )
    first = derive_relationships(snapshot)
    second = derive_relationships(snapshot)
    assert first == second
    keys = [(rel.source_id, rel.target_id, rel.relationship_type.value) for rel in first]
    assert keys == sorted(keys)
    # Lower canonical index is the source within each pair (lambda-a < bucket-a).
    expected = {
        ("lambda-a", "bucket-a", "same_region"),
        ("lambda-a", "bucket-b", "same_region"),
        ("bucket-a", "bucket-b", "same_region"),
    }
    assert keys == sorted(expected)


def test_duplicate_prevention_for_same_pair_and_type():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, account_id="123456789012"),
        _resource("b", SWSResourceType.S3_BUCKET, account_id="123456789012"),
    )
    relationships = derive_relationships(snapshot)
    assert len(relationships) == 1
    assert relationships[0].relationship_type is RelationshipType.SAME_ACCOUNT


def test_empty_snapshot_returns_empty():
    assert derive_relationships(_snapshot()) == []


def test_partial_snapshot_derives_present_resources_only():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, account_id="123456789012"),
        _resource("b", SWSResourceType.S3_BUCKET, account_id="123456789012"),
        partial=True,
    )
    relationships = derive_relationships(snapshot)
    assert len(relationships) == 1
    assert relationships[0].relationship_type is RelationshipType.SAME_ACCOUNT


def test_truncated_snapshot_derives_present_resources_only():
    snapshot = _snapshot(
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, account_id="123456789012"),
        _resource("b", SWSResourceType.S3_BUCKET, account_id="123456789012"),
        truncated=True,
    )
    relationships = derive_relationships(snapshot)
    assert len(relationships) == 1
    assert relationships[0].relationship_type is RelationshipType.SAME_ACCOUNT


def test_input_snapshot_is_not_mutated():
    resources = [
        _resource(LAMBDA_ARN, SWSResourceType.LAMBDA_FUNCTION, account_id="123456789012"),
        _resource("b", SWSResourceType.S3_BUCKET, account_id="123456789012"),
    ]
    snapshot = _snapshot(*resources)
    before = [resource.model_dump() for resource in snapshot.resources]
    derive_relationships(snapshot)
    after = [resource.model_dump() for resource in snapshot.resources]
    assert after == before
    assert snapshot.resources == resources


def test_snapshot_larger_than_safety_cap_raises():
    resources = [
        _resource(f"bucket-{index}", SWSResourceType.S3_BUCKET, account_id="123456789012")
        for index in range(1001)
    ]
    snapshot = _snapshot(*resources)
    with pytest.raises(WorkspaceSnapshotTooLargeError):
        derive_relationships(snapshot)


def test_snapshot_at_safety_cap_derives_instead_of_raising():
    resources = [
        _resource(f"bucket-{index}", SWSResourceType.S3_BUCKET, account_id="123456789012")
        for index in range(1000)
    ]
    snapshot = _snapshot(*resources)
    relationships = derive_relationships(snapshot)
    assert len(relationships) == 1000 * 999 // 2