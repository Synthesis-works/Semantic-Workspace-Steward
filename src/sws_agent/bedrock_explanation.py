"""Optional Strands-backed explanation layer via Bedrock (M3-A).

Fresh SWS module (no SMS reuse). Implements the optional explanation layer
behind the ``ExplanationProvider`` protocol with Strands as the orchestration
layer. SWS functions fully with no LLM provider: ``NullExplanationProvider``
(explanation.py) remains the default, and this provider is only used when a
caller explicitly injects one.

Design contract (approved M3-A):

  - Strands is the intended agent layer. This module never talks to boto3 or
    Bedrock directly; the Strands agent is built by ``build_strands_agent``
    and injected into the provider as a plain callable, so the whole layer is
    hermetic and testable with fakes.
  - One model invocation per ``explain()``: the Strands agent is configured
    with no tools (``tools=[]``) and no retries (``retry_strategy=None``), so
    a call performs exactly one model call, and this provider invokes that
    agent exactly once.
  - Stateless: the provider holds no mutable state and every ``explain()``
    builds a fresh prompt and returns a fresh result.
  - Fail closed: a raising agent or unusable output never propagates; the
    provider returns a null interpreted ``ExplanationResult`` carrying a
    stable reason. Explanations are ``ClaimKind.INTERPRETED`` claims only and
    never influence policy, authorization, or risk decisions.
  - Import safety: ``strands`` is an optional dependency (the ``explanation``
    extra). It is imported lazily inside ``build_strands_agent`` only;
    importing ``sws_agent`` never requires Strands.

The trace records ``TraceEventType.ANALYSIS`` events only. Analysis events are
inert for workspace ``partial`` / ``failures`` semantics, which derive
exclusively from ``TraceEventType.INVENTORY_QUERY`` events.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .constants import ClaimKind, TraceEventType
from .interfaces import ExplanationProvider, TraceSink
from .models import ExplanationResult, PolicyDecision, ResourceRecord

STRANDS_EXPLANATION_PROVIDER: str = "strands"
"""Stable provider identifier recorded on every Strands-backed explanation."""

EXPLANATION_AGENT_UNAVAILABLE: str = "explanation_agent_unavailable"
"""Stable reason when the optional Strands package is not installed."""

EXPLANATION_MODEL_CALL_FAILED: str = "explanation_model_call_failed"
"""Stable reason when the injected agent call raises."""

EXPLANATION_OUTPUT_INVALID: str = "explanation_output_unparseable"
"""Stable reason when the agent returned no usable text."""

EXPLANATION_SYSTEM_PROMPT: str = (
    "You produce natural-language explanations for SWS (Semantic Workspace "
    "Steward). You are given one resource record and one deterministic policy "
    "decision. Explain the decision in plain, direct English. Treat your "
    "explanation as interpretation only: it never overrides observed or "
    "derived facts and it never influences policy, authorization, or risk "
    "decisions. Do not invent costs, IDs, ARNs, account numbers, or AWS facts "
    "that are not present in the input. Reply with the explanation text only."
)
"""Fixed system prompt for the Strands explanation agent."""

_PROMPT_FRAMING: str = (
    "Explain the following SWS resource and its deterministic policy decision "
    "in plain, direct English. The resource and decision are:\n"
)

EXPLANATION_PROMPT_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"raw", "arn", "account_id"}
)
"""Resource fields that must never appear in an explanation prompt.

Referenced by tests to pin the security boundary: the canonical projection
never contains these keys, so raw API payloads and canonical identities do
not reach the model."""


def _project_explanation_input(
    resource: ResourceRecord, decision: PolicyDecision
) -> dict[str, Any]:
    """Deterministic projection of a resource and decision into explainable facts.

    Deliberately omits ``raw``, ``arn``, and ``account_id`` so the prompt never
    carries raw API payloads or canonical identities beyond what the decision
    exposes. The projection shape is stable and is what the deterministic
    ``build_prompt`` serializes.
    """
    resource_facts: dict[str, Any] = {
        "resource_id": resource.resource_id,
        "resource_type": resource.resource_type.value,
        "name": resource.name,
        "region": resource.region,
        "owner_tag": resource.owner_tag,
        "created_at": (
            resource.created_at.isoformat()
            if resource.created_at is not None
            else None
        ),
        "metrics": dict(resource.metrics),
    }
    assert not (set(resource_facts) & EXPLANATION_PROMPT_FORBIDDEN_KEYS)
    return {
        "resource": resource_facts,
        "decision": {
            "resource_id": decision.resource_id,
            "recommended_action": decision.recommended_action.value,
            "risk_level": decision.risk_level.value,
            "rationale": decision.rationale,
            "confidence": decision.confidence,
            "needs_approval": decision.needs_approval,
            "rule": decision.rule,
            "evidence": decision.evidence,
        },
    }


class StrandsUnavailableError(RuntimeError):
    """Raised when Strands is required but ``strands-agents`` is not installed."""


def _project_explanation_input(
    resource: ResourceRecord, decision: PolicyDecision
) -> dict[str, Any]:
    """Deterministic projection of a resource and decision into explainable facts.

    Deliberately omits ``raw``, ``arn``, and ``account_id`` so the prompt never
    carries raw API payloads or canonical identities beyond what the decision
    exposes. The projection shape is stable and is what the deterministic
    ``build_prompt`` serializes.
    """
    return {
        "resource": {
            "resource_id": resource.resource_id,
            "resource_type": resource.resource_type.value,
            "name": resource.name,
            "region": resource.region,
            "owner_tag": resource.owner_tag,
            "created_at": (
                resource.created_at.isoformat()
                if resource.created_at is not None
                else None
            ),
            "metrics": dict(resource.metrics),
        },
        "decision": {
            "resource_id": decision.resource_id,
            "recommended_action": decision.recommended_action.value,
            "risk_level": decision.risk_level.value,
            "rationale": decision.rationale,
            "confidence": decision.confidence,
            "needs_approval": decision.needs_approval,
            "rule": decision.rule,
            "evidence": decision.evidence,
        },
    }


def build_prompt(resource: ResourceRecord, decision: PolicyDecision) -> str:
    """Deterministic prompt for one explanation request.

    The prompt is a fixed framing string plus the incoming facts serialized
    with ``json.dumps(..., sort_keys=True)``, so identical inputs always yield
    identical prompts.
    """
    facts = json.dumps(
        _project_explanation_input(resource, decision), sort_keys=True
    )
    return _PROMPT_FRAMING + facts


def build_strands_agent(model: str) -> Any:
    """Build the Strands explanation agent worth injecting into the provider.

    The ``strands`` package is optional (the ``explanation`` extra) and is
    imported lazily here so ``import sws_agent`` never requires it. Raises
    ``StrandsUnavailableError`` with a stable message when the package is
    absent.

    The returned agent is configured for exactly one model call per prompt:
    no tools, no retries, no default console callback handler, and a
    ``NullConversationManager`` so repeated calls stay stateless.
    """
    try:
        from strands import Agent
        from strands.agent.conversation_manager import NullConversationManager
    except ImportError as exc:  # pragma: no cover - guards the optional extra
        raise StrandsUnavailableError(
            "strands-agents is not installed; install the 'explanation' extra "
            "(pip install sws-agent[explanation])"
        ) from exc
    return Agent(
        model=model,
        tools=[],
        system_prompt=EXPLANATION_SYSTEM_PROMPT,
        name="SWS Explanation Agent",
        description="Explains SWS policy decisions for a single resource.",
        callback_handler=None,
        retry_strategy=None,
        conversation_manager=NullConversationManager(),
    )


def _null_result(reason: str) -> ExplanationResult:
    """A fail-closed interpreted result carrying a stable reason."""
    return ExplanationResult(
        text=None,
        claim_kind=ClaimKind.INTERPRETED,
        provider=STRANDS_EXPLANATION_PROVIDER,
        reason=reason,
    )


class BedrockExplanationProvider:
    """ExplanationProvider delegating to an injected Strands-shaped agent.

    The injected ``agent`` is called as ``agent(prompt)`` exactly once per
    ``explain()`` and must return an object whose ``str()`` is the explanation
    text (Strands ``AgentResult`` satisfies this). A call that raises or
    yields no usable text fails closed into a null interpreted result.
    """

    def __init__(
        self,
        agent: Callable[[str], Any],
        *,
        model: str,
        trace: TraceSink | None = None,
    ) -> None:
        if not callable(agent):
            raise TypeError("agent must be a callable Strands agent")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty model identifier")
        self._agent = agent
        self._model = model
        self._trace = trace

    def _metadata(self) -> dict[str, Any]:
        return {
            "provider": STRANDS_EXPLANATION_PROVIDER,
            "model": self._model,
        }

    def explain(
        self, resource: ResourceRecord, decision: PolicyDecision
    ) -> ExplanationResult:
        prompt = build_prompt(resource, decision)
        if self._trace is not None:
            self._trace.record(
                TraceEventType.ANALYSIS,
                "requesting explanation",
                metadata=self._metadata(),
            )
        try:
            result = self._agent(prompt)
            text = None if result is None else str(result).strip()
        except Exception:
            if self._trace is not None:
                self._trace.fail(
                    TraceEventType.ANALYSIS,
                    "explanation model call failed",
                    metadata={
                        **self._metadata(),
                        "reason": EXPLANATION_MODEL_CALL_FAILED,
                    },
                )
            return _null_result(EXPLANATION_MODEL_CALL_FAILED)

        if not text:
            if self._trace is not None:
                self._trace.fail(
                    TraceEventType.ANALYSIS,
                    "explanation output invalid",
                    metadata={
                        **self._metadata(),
                        "reason": EXPLANATION_OUTPUT_INVALID,
                    },
                )
            return _null_result(EXPLANATION_OUTPUT_INVALID)

        if self._trace is not None:
            self._trace.succeed(
                TraceEventType.ANALYSIS,
                "explanation generated",
                metadata={**self._metadata(), "chars": len(text)},
            )
        return ExplanationResult(
            text=text,
            claim_kind=ClaimKind.INTERPRETED,
            provider=STRANDS_EXPLANATION_PROVIDER,
            reason="",
        )