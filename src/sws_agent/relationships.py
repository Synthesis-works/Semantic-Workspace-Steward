"""Resource relationship resolution boundary.

REUSE DECISION (see docs/reuse-decisions.md):

- Reused (principle): the rule from SMS's relationships.py that
  deterministic evidence takes precedence over semantic inference.
- Rewritten: the implementation is fresh for AWS resource records.

Inferred relationships never override, downgrade, or replace
deterministic evidence for the same source/target/type triple.

M2C-C adds deterministic relationship derivation within a
``WorkspaceSnapshot``: only controlled ``RelationshipType`` members are
produced, only from values that are both present and equal, and the output
is fully deterministic. Derivation never performs semantic inference and
never implies that an absent relationship proves anything when the source
snapshot is partial or truncated.
"""

from __future__ import annotations

from typing import Iterable

from .constants import (
    MAX_RESOURCES_FOR_RELATIONSHIP_DERIVATION,
    ClaimKind,
    EvidenceBasis,
    RelationshipType,
)
from .models import ResourceRecord, ResourceRelationship, WorkspaceSnapshot


class WorkspaceSnapshotTooLargeError(ValueError):
    """Raised when a snapshot exceeds the relationship-derivation safety cap.

    Pairwise derivation is O(n^2); snapshots beyond the cap are refused
    rather than allowed to blow up CPU and output size.
    """


def merge_relationships(
    *,
    deterministic: Iterable[ResourceRelationship],
    inferred: Iterable[ResourceRelationship],
) -> list[ResourceRelationship]:
    """Merge inferred relationships without overriding deterministic ones.

    Deterministic relationships are kept as-is. An inferred relationship
    is only added when no deterministic relationship exists for the same
    (source_id, target_id, relationship_type) triple.
    """
    deterministic_by_key: dict[tuple[str, str, RelationshipType], ResourceRelationship] = {}
    for relationship in deterministic:
        key = (
            relationship.source_id,
            relationship.target_id,
            relationship.relationship_type,
        )
        deterministic_by_key[key] = relationship

    merged: dict[tuple[str, str, RelationshipType], ResourceRelationship] = dict(
        deterministic_by_key
    )
    for relationship in inferred:
        key = (
            relationship.source_id,
            relationship.target_id,
            relationship.relationship_type,
        )
        if key not in merged:
            merged[key] = relationship

    return sorted(
        merged.values(),
        key=lambda r: (r.source_id, r.target_id, r.relationship_type.value),
    )


def _relationship(
    source: ResourceRecord,
    target: ResourceRecord,
    relationship_type: RelationshipType,
    *,
    attribute: str,
    matched_value: str,
) -> ResourceRelationship:
    return ResourceRelationship(
        source_id=source.resource_id,
        target_id=target.resource_id,
        relationship_type=relationship_type,
        basis=EvidenceBasis.DETERMINISTIC,
        claim_kind=ClaimKind.DERIVED,
        evidence=[{"attribute": attribute, "value": matched_value}],
        confidence=1.0,
    )


def _pair_relationships(
    source: ResourceRecord, target: ResourceRecord
) -> list[ResourceRelationship]:
    """Return every controlled relationship that holds between the pair.

    Each predicate requires both values to be present and exactly equal;
    a missing value on either side never produces a relationship. Multiple
    relationship types for the same pair are allowed.
    """
    relationships: list[ResourceRelationship] = []
    if source.account_id is not None and source.account_id == target.account_id:
        relationships.append(
            _relationship(
                source,
                target,
                RelationshipType.SAME_ACCOUNT,
                attribute="account_id",
                matched_value=source.account_id,
            )
        )
    if source.region is not None and source.region == target.region:
        relationships.append(
            _relationship(
                source,
                target,
                RelationshipType.SAME_REGION,
                attribute="region",
                matched_value=source.region,
            )
        )
    if source.owner_tag is not None and source.owner_tag == target.owner_tag:
        relationships.append(
            _relationship(
                source,
                target,
                RelationshipType.SAME_OWNER_TAG,
                attribute="owner_tag",
                matched_value=source.owner_tag,
            )
        )
    return relationships


def derive_relationships(snapshot: WorkspaceSnapshot) -> list[ResourceRelationship]:
    """Derive deterministic relationships among a snapshot's present resources.

    Semantics (approved M2C-C):
    - Only SAME_ACCOUNT, SAME_REGION, and SAME_OWNER_TAG, and only when both
      sides are known and exactly equal; missing values never produce a
      relationship.
    - No self-pairs; each symmetric pair is emitted once.
    - Canonical source/target ordering: snapshot resources are already in
      canonical order, so the lower index is the source and the higher index
      is the target. Direction is deterministic.
    - Multiple relationship types per pair are allowed.
    - Output is deterministically sorted by (source_id, target_id, type).
    - Every relationship is DETERMINISTIC, DERIVED, confidence 1.0, with
      structured evidence identifying the matched attribute.
    - Partial and truncated snapshots produce relationships only among the
      resources present; an absent relationship proves nothing in incomplete
      data (consult snapshot.partial / snapshot.truncated).
    - The input snapshot is never mutated.
    - Snapshots with more than MAX_RESOURCES_FOR_RELATIONSHIP_DERIVATION
      resources raise WorkspaceSnapshotTooLargeError.
    """
    resources = snapshot.resources
    if len(resources) > MAX_RESOURCES_FOR_RELATIONSHIP_DERIVATION:
        raise WorkspaceSnapshotTooLargeError(
            f"snapshot has {len(resources)} resources; relationship derivation "
            f"is capped at {MAX_RESOURCES_FOR_RELATIONSHIP_DERIVATION}"
        )

    relationships: list[ResourceRelationship] = []
    for index in range(len(resources) - 1):
        source = resources[index]
        for target in resources[index + 1:]:
            relationships.extend(_pair_relationships(source, target))

    return sorted(
        relationships,
        key=lambda r: (r.source_id, r.target_id, r.relationship_type.value),
    )