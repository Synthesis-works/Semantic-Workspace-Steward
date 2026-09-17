"""Protocol boundaries between SWS orchestration layers.

Structural convention (Protocols for decoupling) is reused from SMS's
interfaces.py; every interface in this module is written fresh for SWS's
AWS workspace domain.

These boundaries keep the MCP layer thin: business logic implements these
Protocols, and the MCP adapter (future) only adapts them to MCP tools and
resources. Policy, authorization, and action logic never live in the MCP
layer.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import (
    AnalysisReport,
    AuthorizationResult,
    ExplanationResult,
    PolicyDecision,
    ResourceRecord,
)


@runtime_checkable
class TraceSink(Protocol):
    """Append-only execution-event sink."""

    def record(self, event_type: Any, message: str, **kwargs: Any) -> Any: ...
    def succeed(self, event_type: Any, message: str, **kwargs: Any) -> Any: ...
    def fail(self, event_type: Any, message: str, **kwargs: Any) -> Any: ...


@runtime_checkable
class InventoryCollector(Protocol):
    """Collects a bounded inventory of a single AWS resource type.

    Deterministic metadata and metric collection; the collector must
    respect per-request resource limits and pagination.
    """

    def collect(self, *, limit: int | None = None) -> list[ResourceRecord]: ...


@runtime_checkable
class ResourceAnalyzer(Protocol):
    """Produces a semantically framed report for a single resource.

    The semantic layer may explain or interpret; it must not bypass
    policy evaluation or authorization.
    """

    def analyze(self, resource: ResourceRecord) -> AnalysisReport: ...


@runtime_checkable
class PolicyEngine(Protocol):
    """Deterministic policy evaluation.

    Must be independently testable and must never delegate its decision
    to an LLM. "AI understands. Deterministic policy decides."
    """

    def evaluate(self, resource: ResourceRecord) -> PolicyDecision: ...


@runtime_checkable
class ApprovalProvider(Protocol):
    """Human-approval boundary.

    Obtains the required human approval implied by an authorization
    decision and returns the resolved outcome.
    """

    def request_approval(self, decision: PolicyDecision) -> AuthorizationResult: ...


@runtime_checkable
class ActionExecutor(Protocol):
    """Executes a narrowly scoped action and returns the actual outcome.

    Execution must follow: validate target -> validate eligibility ->
    deterministic policy -> approval -> execute -> verify resulting AWS
    state -> record actual outcome. This foundation defines the boundary
    only; no AWS actions are implemented yet.
    """

    def execute(self, resource: ResourceRecord, action: Any) -> Any: ...


@runtime_checkable
class ExplanationProvider(Protocol):
    """Optional natural-language explanation of a resource or decision.

    SWS must function fully when no explanation provider is registered;
    the default NullExplanationProvider returns an ExplanationResult with
    ``text=None``. Explanations are interpreted claims only and never
    influence policy, authorization, or risk decisions.
    """

    def explain(
        self, resource: ResourceRecord, decision: PolicyDecision
    ) -> ExplanationResult: ...