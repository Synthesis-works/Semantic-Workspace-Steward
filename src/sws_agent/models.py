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
    MAX_RESOURCES_PER_INVENTORY_REQUEST,
    ApprovalStatus,
    AuthorizationDecision,
    ClaimKind,
    CollectionFailureCategory,
    EvidenceBasis,
    ExecutionMode,
    PotentialAction,
    RelationshipType,
    RiskLevel,
    SWSResourceType,
)


class ResourceRecord(BaseModel):
    """A discovered AWS resource and its collected metrics.

    ``arn`` and ``account_id`` are additive canonical identity fields added
    in M2C-A. Collectors do not populate them; the canonicalization layer
    (canonical.py) derives them deterministically after collection. Both
    default to ``None`` so existing M2B constructions remain valid.
    """

    resource_id: str = Field(min_length=1)
    resource_type: SWSResourceType
    name: str | None = None
    region: str | None = None
    owner_tag: str | None = None
    created_at: datetime | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    arn: str | None = Field(default=None, min_length=1)
    account_id: str | None = Field(default=None, pattern=r"^[0-9]{12}$")

    @field_validator("resource_type", mode="before")
    @classmethod
    def _normalize_resource_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class ResourceRelationship(BaseModel):
    """A relationship between two AWS resources.

    ``relationship_type`` is a controlled ``RelationshipType`` member;
    free-form labels are rejected. ``bias`` records whether the relationship
    rests on deterministic evidence or semantic inference, and ``claim_kind``
    records whether the relationship is a derived claim. Deterministic
    evidence always takes precedence over inferred claims.
    """

    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    relationship_type: RelationshipType
    basis: EvidenceBasis = EvidenceBasis.DETERMINISTIC
    claim_kind: ClaimKind = ClaimKind.DERIVED
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("relationship_type", mode="before")
    @classmethod
    def _normalize_relationship_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class CollectionFailure(BaseModel):
    """A single failed collection step on an inventory run.

    Built by the workspace builder (workspace.py) from the run-scoped
    FAILED inventory trace events. ``fatal`` is True exactly when the
    primary list operation for a resource type failed. Enrichment and parse
    failures are not fatal, but failure of any kind (fatal or not) makes the
    enclosing snapshot ``partial``: collection did not run completely clean.
    On non-fatal failures the resource record is either preserved with a
    ``None`` field (enrichment) or skipped (parse), and collection continues.

    ``trace_event_id`` links this failure 1:1 to the FAILED trace event it
    was derived from (M2B trace fail events use a per-recorder sequence, so
    an event is uniquely identified by its recorder plus its id).
    """

    resource_type: SWSResourceType
    source: str = Field(min_length=1)
    category: CollectionFailureCategory
    message: str = Field(min_length=1)
    fatal: bool = False
    resource_id: str | None = Field(default=None, min_length=1)
    trace_event_id: int | None = Field(default=None, ge=1)

    @field_validator("resource_type", "category", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class WorkspaceSnapshot(BaseModel):
    """A deterministic snapshot of a workspace's AWS inventory.

    Produced by the workspace builder (workspace.py) after a hermetic
    collection run. The snapshot records intent (``resource_types``,
    ``regions``, the effective per-resource-type ``requested_limit``),
    the canonical resources collected, per-type counts, and the collection
    outcome (``truncated`` / ``partial`` / ``failures``).

    Honesty contract: ``truncated`` is only ever True when the collectors
    observed genuinely more data than they returned (never inferred from
    ``count == limit``), ``partial`` is True whenever any run-scoped
    INVENTORY_QUERY FAILED event exists (fatal primary failures and
    non-fatal enrichment/parse failures alike), and ``failures`` only
    contains events actually recorded in the trace during this run.

    ``cost`` carries workspace-level cost estimates (M2C-E). Cost data is
    deliberately kept separate from ``resources`` (which holds canonical
    ``ResourceRecord`` inventory) and from ``counts``: it is account-level
    aggregated data, never per-resource figures, and it never changes
    ``partial`` semantics on its own beyond the collection-failure contract.
    """

    snapshot_id: str = Field(min_length=1)
    created_at: datetime
    collected_at: datetime | None = None
    run_id: str | None = Field(default=None, min_length=1)
    requested_limit: int = Field(
        default=MAX_RESOURCES_PER_INVENTORY_REQUEST, ge=1
    )
    regions: list[str] = Field(min_length=1)
    resource_types: list[SWSResourceType] = Field(min_length=1)
    resources: list[ResourceRecord] = Field(default_factory=list)
    counts: dict[SWSResourceType, int] = Field(default_factory=dict)
    truncated: bool = False
    partial: bool = False
    failures: list[CollectionFailure] = Field(default_factory=list)
    cost: list[CostEstimate] = Field(default_factory=list)

    @field_validator("resource_types", mode="before")
    @classmethod
    def _normalize_resource_types(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [
                item.strip().lower() if isinstance(item, str) else item
                for item in value
            ]
        return value

    @field_validator("created_at", "collected_at", mode="before")
    @classmethod
    def _require_aware_datetime(cls, value: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("snapshot timestamps must be timezone-aware")
        return value

    @field_validator("regions")
    @classmethod
    def _regions_must_be_non_empty_strings(cls, value: list[str]) -> list[str]:
        for region in value:
            if not isinstance(region, str) or not region.strip():
                raise ValueError("regions must be non-empty strings")
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

    ``rule`` records the stable identifier of the deterministic rule that
    fired (``None`` when no rule triggered), and ``evidence`` carries the
    machine-readable attribute/value facts that the rule's predicate
    depended on. Both fields are additive (M2C-D): existing constructions
    that omit them remain valid, and the engine never executes an action.
    """

    resource_id: str = Field(min_length=1)
    recommended_action: PotentialAction
    risk_level: RiskLevel = RiskLevel.NONE
    rationale: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_approval: bool = False
    rule: str | None = Field(default=None, min_length=1)
    evidence: list[dict[str, Any]] = Field(default_factory=list)

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