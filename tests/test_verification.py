"""M9 outcome-verification taxonomy: facts, ambiguity, and honest unknowns.

The default verifier is deliberately pessimistic:

  - SUCCESS requires every expected fact to be matched by the observation;
  - FAILED requires a direct contradiction (or a confirmed call error);
  - PARTIALLY_VERIFIED reports partial evidence honestly;
  - UNKNOWN is used whenever an observation is unavailable or ambiguous
    (including a timeout) and is NEVER upgraded to SUCCESS.

Provider fakes here represent the observation harness the coordinator would
inject; nothing in this file touches AWS or the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sws_agent.constants import VerificationStatus
from sws_agent.models import ResourceObservation
from sws_agent.verification import (
    DefaultOutcomeVerifier,
    ObservationError,
    ObservationProvider,
    OutcomeVerifier,
)

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _observation(
    facts: dict | None = None, *, ambiguous: bool = False
) -> ResourceObservation:
    return ResourceObservation(
        resource_id="inst-1",
        resource_type="ec2_instance",
        facts=dict(facts or {}),
        observed_at=FIXED_NOW + timedelta(minutes=1),
        ambiguous=ambiguous,
    )


def test_success_only_when_every_expected_fact_matches():
    result = DefaultOutcomeVerifier().verify(
        observation=_observation({"state": "stopped", "state_reason": "user"}),
        expected_facts={"state": "stopped", "state_reason": "user"},
    )
    assert result.status is VerificationStatus.SUCCESS


def test_failed_on_contradiction():
    result = DefaultOutcomeVerifier().verify(
        observation=_observation({"state": "running"}),
        expected_facts={"state": "stopped"},
    )
    assert result.status is VerificationStatus.FAILED


def test_partially_verified_when_expected_fact_unobserved():
    result = DefaultOutcomeVerifier().verify(
        observation=_observation({"state": "stopped"}),
        expected_facts={"state": "stopped", "instance_type": "t3.micro"},
    )
    assert result.status is VerificationStatus.PARTIALLY_VERIFIED


def test_unknown_when_observation_is_ambiguous():
    result = DefaultOutcomeVerifier().verify(
        observation=_observation({"state": "stopped"}, ambiguous=True),
        expected_facts={"state": "stopped"},
    )
    assert result.status is VerificationStatus.UNKNOWN


def test_ambiguous_observation_never_becomes_success_even_when_facts_match():
    result = DefaultOutcomeVerifier().verify(
        observation=_observation({"state": "stopped"}, ambiguous=True),
        expected_facts={"state": "stopped"},
    )
    assert result.status is VerificationStatus.UNKNOWN
    assert result.status is not VerificationStatus.SUCCESS


def test_no_postconditions_never_claimed_success():
    result = DefaultOutcomeVerifier().verify(
        observation=_observation({"state": "stopped"}),
        expected_facts={},
    )
    assert result.status is VerificationStatus.PARTIALLY_VERIFIED


def test_success_reports_matched_details():
    result = DefaultOutcomeVerifier().verify(
        observation=_observation({"state": "stopped"}),
        expected_facts={"state": "stopped"},
    )
    assert any("expected matches observed" in detail for detail in result.details)


def test_default_verifier_satisfies_outcome_verifier_protocol():
    assert isinstance(DefaultOutcomeVerifier(), OutcomeVerifier)


def test_fake_provider_satisfies_observation_provider_protocol():
    class _NoopProvider:
        def observe(self, resource_id: str) -> None:
            del resource_id
            raise ObservationError("unavailable")

    assert isinstance(_NoopProvider(), ObservationProvider)