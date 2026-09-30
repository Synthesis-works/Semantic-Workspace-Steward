"""Outcome verification contracts and the default verifier (M9).

Verification answers a single question after a mutation boundary was
subject to an attempt: does an independent, post-attempt observation of
the target resource support the expected post-state?

The taxonomy is deliberately pessimistic (see ``VerificationStatus`` in
constants.py):

- SUCCESS is only ever reported when the observed facts match every
  expected fact.
- FAILED requires a confirmed contradiction or a confirmed call error.
- UNKNOWN is used whenever the outcome cannot be firmly established (an
  observation is missing, timed out, or otherwise ambiguous). An UNKNOWN
  is NEVER upgraded to SUCCESS, and SWS never blindly retries an ambiguous
  attempt on the same plan.
- PARTIALLY_VERIFIED honestly reports the case where part of the expected
  state was confirmed but part could not be observed.

Boundaries: this module never performs AWS calls and never imports the
AWS SDK. Observation providers are injected by the caller; the default
verifier only compares plain, sanitized facts.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .constants import VerificationStatus
from .models import ResourceObservation, VerificationResult


def _fact_confirms(observed: Any, expected: Any) -> bool:
    """Return True only when ``observed`` confirms ``expected`` exactly.

    Equality alone is not enough: in Python ``True == 1`` and ``False == 0``,
    so a boolean fact would silently satisfy a numeric postcondition (and the
    reverse). A verification claim must not rest on that, so booleans only
    ever confirm booleans.
    """
    if isinstance(observed, bool) or isinstance(expected, bool):
        return isinstance(observed, bool) and isinstance(expected, bool) and observed == expected
    return bool(observed == expected)


class ObservationError(RuntimeError):
    """An observation provider could not establish an authoritative state.

    Raised by providers that would rather fail than return an ambiguous
    result; the coordinator treats a raised observation exactly like an
    unavailable one (verification outcome UNKNOWN, never SUCCESS).
    """


@runtime_checkable
class ObservationProvider(Protocol):
    """Produces an independent post-attempt observation of a resource.

    ``observe`` returns ``None`` when no observation is available for the
    resource (for example no read-only verification primitive exists yet)
    and raises ``ObservationError`` when the authoritative state could not
    be established. Providers never fabricate state.
    """

    def observe(self, resource_id: str) -> ResourceObservation | None: ...


@runtime_checkable
class OutcomeVerifier(Protocol):
    """Compares an observation against the expected post-state facts."""

    def verify(
        self,
        *,
        observation: ResourceObservation,
        expected_facts: dict[str, Any],
    ) -> VerificationResult: ...


class DefaultOutcomeVerifier:
    """Deterministic fact comparison behind the ``OutcomeVerifier`` protocol.

    Comparison is exact set matching over plain fact values:

      - ambiguous observation -> UNKNOWN (never SUCCESS, never FAILED);
      - no expected facts     -> PARTIALLY_VERIFIED (SWS never fabricates a
        success claim with nothing to check);
      - any contradicted fact -> FAILED;
      - confirmed facts but some expected facts unobserved ->
        PARTIALLY_VERIFIED;
      - every expected fact confirmed, none contradicted -> SUCCESS.

    M10: the expected facts are supplied by the coordinator from the
    action's canonical postcondition, never from the caller, and a fact only
    counts as confirmed when its value *and* its boolean-ness agree. Plain
    Python equality would let ``True`` satisfy an expected ``1`` (and ``False``
    an expected ``0``), which would let a type-confused fact claim success.
    """

    def verify(
        self,
        *,
        observation: ResourceObservation,
        expected_facts: dict[str, Any],
    ) -> VerificationResult:
        observed_facts = dict(observation.facts)
        details: list[str] = []
        if observation.ambiguous:
            return VerificationResult(
                status=VerificationStatus.UNKNOWN,
                expected_facts=dict(expected_facts),
                observed_facts=observed_facts,
                details=["observation is ambiguous; outcome cannot be established"],
                observed_at=observation.observed_at,
            )
        if not expected_facts:
            return VerificationResult(
                status=VerificationStatus.PARTIALLY_VERIFIED,
                expected_facts={},
                observed_facts=observed_facts,
                details=["no postconditions to verify; cannot claim success"],
                observed_at=observation.observed_at,
            )
        contradicted: list[str] = []
        matched: list[str] = []
        missing: list[str] = []
        for key, expected_value in expected_facts.items():
            if key not in observed_facts:
                missing.append(key)
                continue
            if _fact_confirms(observed_facts[key], expected_value):
                matched.append(key)
            else:
                contradicted.append(key)
        for key in matched:
            details.append(f"{key}: expected matches observed")
        for key in missing:
            details.append(f"{key}: expected but not observed")
        for key in contradicted:
            details.append(f"{key}: observed contradicts expected")
        if contradicted:
            status = VerificationStatus.FAILED
        elif missing:
            status = VerificationStatus.PARTIALLY_VERIFIED
        else:
            status = VerificationStatus.SUCCESS
        return VerificationResult(
            status=status,
            expected_facts=dict(expected_facts),
            observed_facts=observed_facts,
            details=details,
            observed_at=observation.observed_at,
        )