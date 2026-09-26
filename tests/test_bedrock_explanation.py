"""Strands-backed explanation provider (M3-A): hermetic fakes only.

Covers the BedrockExplanationProvider contract with an injected Strands-shaped
fake agent: exactly one invocation per explain(), fail-closed behavior on agent
failure or unusable output, INTERPRETED-only claims, immutability of inputs,
deterministic prompt construction, statelessness, configuration validation,
and ANALYSIS-only tracing. No AWS, Bedrock, or Strands imports anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sws_agent.bedrock_explanation import (
    EXPLANATION_AGENT_UNAVAILABLE,
    EXPLANATION_MODEL_CALL_FAILED,
    EXPLANATION_OUTPUT_INVALID,
    EXPLANATION_PROMPT_FORBIDDEN_KEYS,
    STRANDS_EXPLANATION_PROVIDER,
    BedrockExplanationProvider,
    StrandsUnavailableError,
    build_prompt,
    build_strands_agent,
)
from sws_agent.constants import (
    ClaimKind,
    PotentialAction,
    RiskLevel,
    SWSResourceType,
    TraceEventType,
)
from sws_agent.explanation import NULL_EXPLANATION_REASON, NullExplanationProvider
from sws_agent.interfaces import ExplanationProvider
from sws_agent.models import (
    ExplanationResult,
    PolicyDecision,
    ResourceRecord,
)
from sws_agent.trace import TraceRecorder

RESOURCE = ResourceRecord(
    resource_id="bucket-example",
    resource_type=SWSResourceType.S3_BUCKET,
    name="example-bucket",
    region="us-east-1",
    owner_tag="eng",
    created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    metrics={"size_gb": 42.0, "objects": 1000.0},
    raw={"secret_payload": "DO_NOT_LEAK"},
    arn="arn:aws:s3:::example-bucket",
    account_id="123456789012",
)
DECISION = PolicyDecision(
    resource_id="bucket-example",
    recommended_action=PotentialAction.FLAG_FOR_REVIEW,
    risk_level=RiskLevel.MEDIUM,
    rationale="Owner tag matches, but the bucket is publicly readable.",
    confidence=0.9,
    needs_approval=False,
    rule="publicly_readable",
    evidence=[{"attribute": "public_access", "value": "true"}],
)


class _RecordingAgent:
    """Strands-Agent-shaped fake that records every invocation."""

    def __init__(self, response: object | None = "explanation text") -> None:
        self.response = response
        self.error: Exception | None = None
        self.calls: list[str] = []

    def __call__(self, prompt: str) -> object | None:
        self.calls.append(prompt)
        if self.error is not None:
            raise self.error
        return self.response


def _provider(
    agent: _RecordingAgent,
    *,
    model: str = "us.anthropic.claude-3.7-sonnet",
    trace: TraceRecorder | None = None,
) -> BedrockExplanationProvider:
    return BedrockExplanationProvider(agent, model=model, trace=trace)


def test_bedrock_provider_satisfies_explanation_provider_protocol():
    agent = _RecordingAgent()
    assert isinstance(_provider(agent), ExplanationProvider)


def test_bedrock_provider_returns_interpreted_explanation_on_happy_path():
    agent = _RecordingAgent("This bucket is safe to keep.")
    result = _provider(agent).explain(RESOURCE, DECISION)
    assert isinstance(result, ExplanationResult)
    assert result.text == "This bucket is safe to keep."
    assert result.claim_kind is ClaimKind.INTERPRETED
    assert result.provider == STRANDS_EXPLANATION_PROVIDER
    assert result.reason == ""


def test_bedrock_provider_invokes_agent_exactly_once():
    agent = _RecordingAgent("explained")
    _provider(agent).explain(RESOURCE, DECISION)
    assert len(agent.calls) == 1


def test_bedrock_provider_passes_deterministic_prompt_to_agent():
    agent = _RecordingAgent("explained")
    _provider(agent).explain(RESOURCE, DECISION)
    assert agent.calls[0] == build_prompt(RESOURCE, DECISION)


def test_bedrock_provider_fails_closed_when_agent_raises():
    agent = _RecordingAgent()
    agent.error = RuntimeError("model unavailable")
    result = _provider(agent).explain(RESOURCE, DECISION)
    assert result.text is None
    assert result.claim_kind is ClaimKind.INTERPRETED
    assert result.provider == STRANDS_EXPLANATION_PROVIDER
    assert result.reason == EXPLANATION_MODEL_CALL_FAILED
    assert len(agent.calls) == 1


def test_bedrock_provider_fails_closed_on_none_output():
    agent = _RecordingAgent(None)
    result = _provider(agent).explain(RESOURCE, DECISION)
    assert result.text is None
    assert result.reason == EXPLANATION_OUTPUT_INVALID


def test_bedrock_provider_fails_closed_on_blank_output():
    agent = _RecordingAgent("   \n\t  ")
    result = _provider(agent).explain(RESOURCE, DECISION)
    assert result.text is None
    assert result.reason == EXPLANATION_OUTPUT_INVALID


def test_conflicting_explanation_never_changes_claim_kind_or_inputs():
    agent = _RecordingAgent("The true risk is HIGH and this decision is wrong.")
    resource_before = RESOURCE.model_dump()
    decision_before = DECISION.model_dump()
    result = _provider(agent).explain(RESOURCE, DECISION)
    assert result.claim_kind is ClaimKind.INTERPRETED
    assert result.claim_kind is not ClaimKind.DERIVED
    assert result.claim_kind is not ClaimKind.OBSERVED
    assert RESOURCE.model_dump() == resource_before
    assert DECISION.model_dump() == decision_before


def test_resource_record_unchanged_after_explain():
    original = RESOURCE.model_dump()
    _provider(_RecordingAgent()).explain(RESOURCE, DECISION)
    assert RESOURCE.model_dump() == original


def test_policy_decision_unchanged_after_explain():
    original = DECISION.model_dump()
    _provider(_RecordingAgent()).explain(RESOURCE, DECISION)
    assert DECISION.model_dump() == original


def test_prompt_is_deterministic():
    first = build_prompt(RESOURCE, DECISION)
    second = build_prompt(RESOURCE, DECISION)
    assert first == second
    agent_a = _RecordingAgent()
    agent_b = _RecordingAgent()
    _provider(agent_a).explain(RESOURCE, DECISION)
    _provider(agent_b).explain(RESOURCE, DECISION)
    assert agent_a.calls[0] == agent_b.calls[0]


def test_prompt_never_exposes_raw_arn_or_account_id():
    prompt = build_prompt(RESOURCE, DECISION)
    for forbidden in EXPLANATION_PROMPT_FORBIDDEN_KEYS:
        assert forbidden not in prompt
    assert "DO_NOT_LEAK" not in prompt
    assert "arn:aws:s3:::example-bucket" not in prompt
    assert "123456789012" not in prompt
    assert "bucket-example" in prompt
    assert "flag_for_review" in prompt
    assert "medium" in prompt


def test_prompt_carries_expected_facts():
    prompt = build_prompt(RESOURCE, DECISION)
    assert '"owner_tag": "eng"' in prompt
    assert '"metrics": {"objects": 1000.0, "size_gb": 42.0}' in prompt
    assert '"rule": "publicly_readable"' in prompt
    assert '"evidence"' in prompt


def test_provider_is_stateless_across_calls():
    agent = _RecordingAgent("same explanation")
    provider = _provider(agent)
    first = provider.explain(RESOURCE, DECISION)
    second = provider.explain(RESOURCE, DECISION)
    assert first == second
    assert len(agent.calls) == 2
    assert agent.calls[0] == agent.calls[1]


def test_invalid_agent_configuration_is_rejected():
    with pytest.raises(TypeError):
        BedrockExplanationProvider(agent="not callable", model="us.anthropic.claude")
    with pytest.raises(ValueError):
        _provider(_RecordingAgent(), model="")
    with pytest.raises(ValueError):
        _provider(_RecordingAgent(), model="   ")
    with pytest.raises(ValueError):
        _provider(_RecordingAgent(), model=None)  # type: ignore[arg-type]


def test_success_records_analysis_start_and_succeeded_only():
    trace = TraceRecorder()
    result = _provider(_RecordingAgent("explained"), trace=trace).explain(
        RESOURCE, DECISION
    )
    assert result.text == "explained"
    analysis = [e for e in trace if e.event_type is TraceEventType.ANALYSIS]
    assert [e.status.value for e in analysis] == ["running", "succeeded"]
    assert not [
        e for e in trace if e.event_type is not TraceEventType.ANALYSIS
    ]
    started, succeeded = analysis
    assert started.metadata["provider"] == STRANDS_EXPLANATION_PROVIDER
    assert succeeded.metadata["model"] == "us.anthropic.claude-3.7-sonnet"
    assert succeeded.metadata["chars"] == len("explained")


def test_failure_records_analysis_failed_event():
    trace = TraceRecorder()
    agent = _RecordingAgent()
    agent.error = RuntimeError("boom")
    result = _provider(agent, trace=trace).explain(RESOURCE, DECISION)
    assert result.reason == EXPLANATION_MODEL_CALL_FAILED
    analysis = [e for e in trace if e.event_type is TraceEventType.ANALYSIS]
    assert [e.status.value for e in analysis] == ["running", "failed"]
    assert analysis[1].metadata["reason"] == EXPLANATION_MODEL_CALL_FAILED
    assert not [
        e for e in trace if e.event_type is TraceEventType.INVENTORY_QUERY
    ]


def test_invalid_output_records_failed_with_reason():
    trace = TraceRecorder()
    result = _provider(_RecordingAgent("  "), trace=trace).explain(RESOURCE, DECISION)
    assert result.reason == EXPLANATION_OUTPUT_INVALID
    analysis = [e for e in trace if e.event_type is TraceEventType.ANALYSIS]
    assert [e.status.value for e in analysis] == ["running", "failed"]
    assert analysis[1].metadata["reason"] == EXPLANATION_OUTPUT_INVALID


def test_no_trace_sink_means_no_events_needed():
    result = _provider(_RecordingAgent("explained")).explain(RESOURCE, DECISION)
    assert result.text == "explained"


def test_null_provider_remains_distinct_default():
    null = NullExplanationProvider().explain(RESOURCE, DECISION)
    assert null.text is None
    assert null.provider == "null"
    assert null.reason == NULL_EXPLANATION_REASON


def test_build_strands_agent_raises_when_strands_absent(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _block_strands(name, *args, **kwargs):
        if name == "strands" or name.startswith("strands."):
            raise ImportError("no module named 'strands'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_strands)
    with pytest.raises(StrandsUnavailableError) as excinfo:
        build_strands_agent("us.anthropic.claude-3.7-sonnet")
    assert "strands-agents is not installed" in str(excinfo.value)
    assert str(excinfo.value) != ""
    assert EXPLANATION_AGENT_UNAVAILABLE  # reason code exists and is stable