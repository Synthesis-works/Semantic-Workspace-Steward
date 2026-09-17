"""Protocol boundaries are satisfied by concrete implementations.

Uses runtime_checkable Protocols so compliance can be asserted at test
time with lightweight fakes and the real modules.
"""

from __future__ import annotations

from sws_agent.authorization import ActionAuthorizer
from sws_agent.interfaces import (
    ApprovalProvider,
    PolicyEngine,
    ResourceAnalyzer,
    TraceSink,
)
from sws_agent.trace import TraceRecorder


class _FakeAnalyzer:
    def analyze(self, resource):
        return None


class _FakePolicyEngine:
    def evaluate(self, resource):
        return None


class _FakeApprovalProvider:
    def request_approval(self, decision):
        return None


def test_trace_recorder_satisfies_trace_sink_protocol():
    assert isinstance(TraceRecorder(), TraceSink)


def test_fake_analyzer_satisfies_resource_analyzer_protocol():
    assert isinstance(_FakeAnalyzer(), ResourceAnalyzer)


def test_fake_policy_engine_satisfies_policy_engine_protocol():
    assert isinstance(_FakePolicyEngine(), PolicyEngine)


def test_fake_approval_provider_satisfies_approval_provider_protocol():
    assert isinstance(_FakeApprovalProvider(), ApprovalProvider)


def test_authorizer_is_not_mistaken_for_policy_engine():
    """The deterministic authorizer is a separate boundary from policy."""
    assert not isinstance(ActionAuthorizer(), PolicyEngine)