"""Fresh SWS domain models for AWS workspace stewardship.

Written from scratch for SWS; not a copy of the SMS domain records.

Structural ideas retained from SMS (documented in docs/reuse-decisions.md):

- Pydantic models with case-insensitive enum normalization at the
  construction boundary and bounded ``ge`` / ``le`` fields.
- The "honest estimate" convention: cost figures are labeled as
  projections with surfaced assumptions and are never an authorization
  gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .constants import (
    ApprovalStatus,
    AuthorizationDecision,
    ClaimKind,
    EvidenceBasis,
    ExecutionMode,
    PotentialAction,
    RiskLevel,
    SWSResourceType,
)


class ResourceRecord(BaseModel):
    """A discovered AWS resource and its collected metrics."""

    resource_id: str = Field(min_length=1)
    resource_type: SWSResourceType
    name: str | None = None
    region: str | None = None
    owner_tag: str | None = None
    created_at: datetime | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("resource_type", mode="before")
    @classmethod
    def _normalize_resource_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class ResourceRelationship(BaseModel):
    """A relationship between two AWS resources.

    ``bias`` records whether the relationship rests on deterministic
    evidence or semantic inference. Deterministic evidence always takes
    precedence over inferred claims.
    """

    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    relationship_type: str = Field(min_length=1)
    basis: EvidenceBasis = EvidenceBasis.DETERMINISTIC
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("relationship_type", mode="before")
    @classmethod
    def _normalize_relationship_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class CostEstimate(BaseModel):
    """A labeled cost estimate.

    Follows the honest-estimate convention: every figure is marked as an
    estimate/projection (``projected``), carries its basis, and surfaces
    assumptions. Estimates are never used as an authorization gate.
    """

    line_item: str = Field(min_length=1)
    amount_usd: float = Field(ge=0.0)
    basis: str = Field(min_length=1)
    projected: bool = True
    assumptions: list[str] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    """Output of deterministic policy evaluation.

    Produced by the deterministic policy engine (interface in
    interfaces.py), which must never delegate decisions to an LLM.
    """

    resource_id: str = Field(min_length=1)
    recommended_action: PotentialAction
    risk_level: RiskLevel = RiskLevel.NONE
    rationale: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_approval: bool = False

    @field_validator("recommended_action", "risk_level", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class AuthorizationRequest(BaseModel):
    """Input to the deterministic authorization gate."""

    resource_id: str = Field(min_length=1)
    resource_type: SWSResourceType
    action: PotentialAction
    execution_mode: ExecutionMode

    @field_validator("resource_type", "action", "execution_mode", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class AuthorizationResult(BaseModel):
    """Outcome of the deterministic authorization gate."""

    decision: AuthorizationDecision
    reason: str = ""
    requires_human_approval: bool = False


class AnalysisReport(BaseModel):
    """A resource analysis produced by the semantic layer.

    The semantic layer may explain or interpret; it must not bypass
    policy evaluation or authorization.
    """

    resource_id: str = Field(min_length=1)
    summary: str = ""
    observations: list[str] = Field(default_factory=list)
    cost_estimates: list[CostEstimate] = Field(default_factory=list)


class ApprovalTicket(BaseModel):
    """A human-approval ticket awaiting a decision.

    Tickets model the human-approval boundary for future side-effecting
    actions. A ticket enters the store as PENDING and may transition
    exactly once to GRANTED, DENIED, or EXPIRED. Ticket creation and
    transition are managed by the approval store (approval.py); this model
    only validates the ticket's shape and enforces case-insensitive
    normalization at the construction boundary.
    """

    ticket_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    action: PotentialAction
    rationale: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str = ""
    decision_reason: str = ""

    @field_validator("action", "status", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class ExplanationResult(BaseModel):
    """Output of the optional natural-language explanation layer.

    A null result carries ``text=None`` with a reason when no LLM provider
    is configured; the rest of SWS must function fully in that state.
    Explanations are always ``ClaimKind.INTERPRETED`` and never override
    derived or observed claims.
    """

    text: str | None = None
    claim_kind: ClaimKind = ClaimKind.INTERPRETED
    provider: str = Field(min_length=1)
    reason: str = ""

    @field_validator("claim_kind", mode="before")
    @classmethod
    def _normalize_claim_kind(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value