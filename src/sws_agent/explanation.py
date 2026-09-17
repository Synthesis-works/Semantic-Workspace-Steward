"""Optional natural-language explanation layer.

Default implementation ships a hermetic null provider so SWS functions
fully with no LLM configured. Real providers are added later behind the
ExplanationProvider protocol and must return interpreted claims only;
they must never influence policy, authorization, or risk decisions.
"""

from __future__ import annotations

from .constants import ClaimKind
from .interfaces import ExplanationProvider
from .models import ExplanationResult, PolicyDecision, ResourceRecord

NULL_EXPLANATION_REASON = "no_llm_configured"
"""Reason attached to null explanations when no LLM provider is available."""


class NullExplanationProvider:
    """ExplanationProvider that always returns a null explanation.

    Used as the default provider so every tool and test path behaves
    identically with or without an LLM. The provider name is recorded in
    the result for traceability.
    """

    def explain(
        self, resource: ResourceRecord, decision: PolicyDecision
    ) -> ExplanationResult:
        return ExplanationResult(
            text=None,
            claim_kind=ClaimKind.INTERPRETED,
            provider="null",
            reason=NULL_EXPLANATION_REASON,
        )


assert isinstance(NullExplanationProvider(), ExplanationProvider)