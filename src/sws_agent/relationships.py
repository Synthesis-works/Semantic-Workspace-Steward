"""Resource relationship resolution boundary.

REUSE DECISION (see docs/reuse-decisions.md):

- Reused (principle): the rule from SMS's relationships.py that
  deterministic evidence takes precedence over semantic inference.
- Rewritten: the implementation is fresh for AWS resource records.

Inferred relationships never override, downgrade, or replace
deterministic evidence for the same source/target/type triple.
"""

from __future__ import annotations

from typing import Iterable

from .models import ResourceRelationship


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
    deterministic_by_key: dict[tuple[str, str, str], ResourceRelationship] = {}
    for relationship in deterministic:
        key = (relationship.source_id, relationship.target_id, relationship.relationship_type)
        deterministic_by_key[key] = relationship

    merged: dict[tuple[str, str, str], ResourceRelationship] = dict(deterministic_by_key)
    for relationship in inferred:
        key = (relationship.source_id, relationship.target_id, relationship.relationship_type)
        if key not in merged:
            merged[key] = relationship

    return sorted(
        merged.values(),
        key=lambda r: (r.source_id, r.target_id, r.relationship_type),
    )