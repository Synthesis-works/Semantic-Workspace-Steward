"""Deterministic evidence takes precedence over semantic inference."""

from __future__ import annotations

from sws_agent.constants import EvidenceBasis
from sws_agent.models import ResourceRelationship
from sws_agent.relationships import merge_relationships


def _relationship(source, target, kind, basis, confidence=1.0):
    return ResourceRelationship(
        source_id=source,
        target_id=target,
        relationship_type=kind,
        basis=basis,
        confidence=confidence,
    )


def test_inferred_relationship_never_overrides_deterministic():
    deterministic = _relationship("a", "b", "uses", EvidenceBasis.DETERMINISTIC)
    inferred = _relationship("a", "b", "uses", EvidenceBasis.INFERRED)
    merged = merge_relationships(deterministic=[deterministic], inferred=[inferred])
    assert len(merged) == 1
    assert merged[0].basis is EvidenceBasis.DETERMINISTIC


def test_inferred_relationship_added_when_no_deterministic_exists():
    inferred = _relationship("a", "b", "related", EvidenceBasis.INFERRED)
    merged = merge_relationships(deterministic=[], inferred=[inferred])
    assert len(merged) == 1
    assert merged[0].basis is EvidenceBasis.INFERRED


def test_stable_sorted_output():
    deterministic = _relationship("a", "b", "uses", EvidenceBasis.DETERMINISTIC)
    inferred = _relationship("a", "b", "related", EvidenceBasis.INFERRED)
    merged = merge_relationships(deterministic=[deterministic], inferred=[inferred])
    assert [rel.relationship_type for rel in merged] == ["related", "uses"]