"""Null explanation provider: the hermetic no-LLM default.

Verifies the optional explanation layer degrades gracefully: with no LLM
configured every SWS path still returns a typed ExplanationResult.
"""

from __future__ import annotations

from sws_agent.constants import ClaimKind
from sws_agent.explanation import NULL_EXPLANATION_REASON, NullExplanationProvider
from sws_agent.interfaces import ExplanationProvider
from sws_agent.models import (
    ExplanationResult,
    PolicyDecision,
    ResourceRecord,
)
from sws_agent.constants import PotentialAction, SWSResourceType

RESOURCE = ResourceRecord(
    resource_id="bucket-example", resource_type=SWSResourceType.S3_BUCKET
)
DECISION = PolicyDecision(
    resource_id="bucket-example",
    recommended_action=PotentialAction.LEAVE,
    rationale="no risk identified",
)


def test_null_provider_returns_typed_null_result():
    result = NullExplanationProvider().explain(RESOURCE, DECISION)
    assert isinstance(result, ExplanationResult)
    assert result.text is None
    assert result.claim_kind is ClaimKind.INTERPRETED
    assert result.provider == "null"
    assert result.reason == NULL_EXPLANATION_REASON


def test_null_provider_never_claims_derived_or_observed():
    result = NullExplanationProvider().explain(RESOURCE, DECISION)
    assert result.claim_kind is ClaimKind.INTERPRETED
    assert result.claim_kind is not ClaimKind.DERIVED
    assert result.claim_kind is not ClaimKind.OBSERVED


def test_null_provider_satisfies_explanation_provider_protocol():
    provider = NullExplanationProvider()
    assert isinstance(provider, ExplanationProvider)


def test_null_provider_is_stateless_across_calls():
    provider = NullExplanationProvider()
    first = provider.explain(RESOURCE, DECISION)
    second = provider.explain(RESOURCE, DECISION)
    assert first == second == provider.explain(RESOURCE, DECISION)


def test_null_result_is_constructible_as_explanation_result():
    result = ExplanationResult(
        text=None,
        claim_kind=ClaimKind.INTERPRETED,
        provider="null",
        reason=NULL_EXPLANATION_REASON,
    )
    assert result.text is None
    assert result.provider == "null"