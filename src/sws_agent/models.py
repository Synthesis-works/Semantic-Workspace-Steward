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

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from .constants import (
    MAX_RESOURCES_PER_INVENTORY_REQUEST,
    ApprovalStatus,
    AuthorizationDecision,
    ClaimKind,
    CollectionFailureCategory,
    EvidenceBasis,
    ExecutionMode,
    ExecutionOutcome,
    ObservationProvenance,
    PotentialAction,
    RefusalReason,
    RelationshipType,
    RiskLevel,
    SWSResourceType,
    VerificationStatus,
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


class CostCollectionReport(BaseModel):
    """Honest summary of a standalone cost-collection run (M7).

    Produced by the MCP backend for the ``get_cost_estimates`` path: it
    carries the estimate rows plus the same completeness contract the
    workspace snapshot applies to cost. ``truncated`` is True only when the
    collector observed genuinely more Cost Explorer pages than it returned,
    and ``failures`` maps the run's FAILED INVENTORY_QUERY trace events 1:1.
    The standalone tool never hides a truncation or a failure.
    """

    estimates: list[CostEstimate] = Field(default_factory=list)
    truncated: bool = False
    failures: list[CollectionFailure] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    """Output of deterministic policy evaluation.

    Produced by the deterministic policy engine (interface in
    interfaces.py), which must never delegate decisions to an LLM.

    ``rule`` records the stable identifier of the deterministic rule that
    fired (``None`` when no rule triggered), and ``evidence`` carries the
    machine-readable attribute/value facts that the rule's predicate
    depended on. Both fields are additive (M2C-D): existing constructions
    that omit them remain valid, and the engine never executes an action.

    M8 lineage (additive): ``snapshot_id`` / ``run_id`` name the collection
    run whose snapshot this decision was derived from, and ``decision_id``
    is the stable identifier of the decision. The deterministic policy
    engine (policy.py) sets ``decision_id`` deterministically from the
    decision's identity inputs (snapshot id, resource id, rule), so
    re-evaluating the same snapshot is a repeatable fact; a default value
    keeps direct model constructions valid.
    """

    resource_id: str = Field(min_length=1)
    recommended_action: PotentialAction
    risk_level: RiskLevel = RiskLevel.NONE
    rationale: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_approval: bool = False
    rule: str | None = Field(default=None, min_length=1)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    decision_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    snapshot_id: str | None = Field(default=None, min_length=1)
    run_id: str | None = Field(default=None, min_length=1)

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
    """A human-approval ticket and its durable lineage (M12).

    A ticket is the record of a human decision to permit one specific
    side-effecting intent. Ticket creation and every state transition are
    managed by the approval store (``approval.py``); this model validates the
    ticket's shape, normalizes case-insensitive inputs at the construction
    boundary, and enforces the status/consumed invariant.

    M12 lifecycle. ``status`` is the single authoritative representation of
    the ticket's state. The legal transitions are:

        PENDING -> GRANTED | DENIED | EXPIRED | REVOKED
        GRANTED -> CONSUMED | EXPIRED | REVOKED

    ``DENIED``, ``EXPIRED``, ``CONSUMED``, and ``REVOKED`` are terminal.

    ``consumed`` is retained as a **compatibility mirror** of
    ``status is CONSUMED``, not as an independent flag. M9 modelled
    consumption as an orthogonal boolean, which left a redeemed approval
    reporting ``GRANTED`` and therefore indistinguishable from a live one by
    status alone. The mirror is preserved because the ticket model is
    serialized into audit payloads; a model validator below rejects any
    construction where the two disagree, so no caller can observe or create a
    ``CONSUMED`` ticket that still claims to be ``GRANTED``.

    M12 lineage additions, all optional and additive:

    * ``revision`` -- monotonic counter starting at 0 on issue and advanced by
      exactly 1 on every committed transition. It is the precondition value
      for the transactional compare-and-swap that M12 Phase 3 adds to the
      durable store; the in-memory store advances it deterministically but
      performs no CAS.
    * ``execution_intent_key`` -- the M10 canonical intent key
      (``execution.execution_intent_key``, a digest over snapshot id, resource
      id, and action) that this ticket authorizes. Phase 1 adds the field
      only; stamping it from the plan is Phase 5, so it is ``None`` for every
      ticket created by the current planner.
    * ``evidence_digest`` -- reserved for the digest of the evidence the
      approver actually saw. Phase 1 adds no hashing scheme and leaves this
      ``None``; Phase 5 defines the canonical representation.
    * ``execution_deadline`` -- the instant after which a ``GRANTED`` ticket
      expires. This is deliberately distinct from the store's decision TTL,
      which bounds only how long a ticket may await a decision. M9 applied no
      upper bound to a grant at all.

    ``plan_id`` (M8, optional) links the ticket to the ``ActionPlan`` that
    created it; a ticket created outside the planner has no plan.
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
    plan_id: str | None = Field(default=None, min_length=1)
    consumed: bool = False
    revision: int = Field(default=0, ge=0)
    execution_intent_key: str | None = Field(default=None, min_length=1)
    evidence_digest: str | None = Field(default=None, min_length=1)
    execution_deadline: datetime | None = None

    @field_validator("action", "status", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @model_validator(mode="after")
    def _consumed_mirrors_status(self) -> ApprovalTicket:
        """Reject any ticket whose ``consumed`` flag contradicts ``status``.

        ``status`` is authoritative. Without this invariant a caller could
        construct a ``CONSUMED`` ticket that still reports ``consumed=False``
        (and so appears redeemable) or a ``GRANTED`` ticket that reports
        ``consumed=True`` (and so appears already redeemed). Either mistake
        would let the execution gate misjudge a live approval.
        """
        expected = self.status is ApprovalStatus.CONSUMED
        if self.consumed is not expected:
            raise ValueError(
                "consumed must mirror status: "
                f"status={self.status.value!r} requires consumed={expected!r}, "
                f"got consumed={self.consumed!r}"
            )
        return self

    @model_validator(mode="after")
    def _granted_requires_execution_deadline(self) -> ApprovalTicket:
        """Reject a ``GRANTED`` ticket that carries no ``execution_deadline``.

        A granted approval is only bounded by its execution deadline: that is
        the only thing standing between it and never expiring. Without the
        field the ticket can never lapse, so any damage that drops the value --
        a partial restore, a bad migration, a torn page write -- would silently
        widen a bounded approval into a permanent one and let it be redeemed
        indefinitely. M9 shipped exactly that fail-open shape.

        Enforcing it on the model means neither store can represent the state:
        ``InMemoryApprovalStore.grant`` and ``DurableApprovalStore.grant`` both
        stamp a deadline, and a row read back from the ledger is rejected before
        the execution gate ever sees it.
        """
        if self.status is ApprovalStatus.GRANTED and self.execution_deadline is None:
            raise ValueError(
                "a GRANTED approval must carry execution_deadline: without it "
                "the ticket can never expire, so damage that dropped the value "
                "would widen a bounded approval into a permanent one"
            )
        return self


class ActionPlan(BaseModel):
    """Output of the pre-execution action-planning workflow (M7).

    A deterministic plan for one candidate action: the authorization
    decision rendered from the configured execution mode and, whenever the
    gate requires human approval, the created PENDING approval ticket.
    ``executed`` is always False: SWS plans and authorizes but never
    executes an AWS action (no executor exists yet).

    M8 lineage (additive): every plan carries a stable ``action_plan_id``
    and an aware ``created_at``; the planner stamps both from injectable
    identity/clock sources so each plan record is uniquely referenceable in
    the durable audit ledger.

    M9 lineage (additive): when the planner is handed the ``PolicyDecision``
    that motivated the plan, it stamps ``decision_id`` / ``snapshot_id`` /
    ``run_id`` from that decision so the plan can be correlated back to the
    exact policy evaluation and collection run it was derived from. All
    three are optional: direct constructions and planner flows that do not
    supply a decision remain valid.
    """

    resource_id: str = Field(min_length=1)
    action: PotentialAction
    execution_mode: ExecutionMode
    authorization: AuthorizationResult
    ticket: ApprovalTicket | None = None
    executed: bool = False
    action_plan_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    decision_id: str | None = Field(default=None, min_length=1)
    snapshot_id: str | None = Field(default=None, min_length=1)
    run_id: str | None = Field(default=None, min_length=1)

    @field_validator("action", "execution_mode", mode="before")
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


class ResourceObservation(BaseModel):
    """An independent observation of a resource's state (M9, hardened in M10).

    Produced by an observation provider. ``facts`` carries sanitized,
    machine-readable attribute/value facts (for example
    ``{"state": "stopped"}``); it never carries credentials or secrets.
    ``ambiguous`` is set by the observer when the authoritative state could
    not be firmly established (for example the observation timed out); an
    ambiguous observation can never support a SUCCESS claim.

    ``arn`` / ``account_id`` / ``region`` are canonical identity facts. M10
    makes them **mandatory for the execution gate**: a refusal is issued when
    either the snapshot record or the observation lacks one, instead of
    silently skipping the comparison.

    ``provenance`` (M10) records who minted the observation. It defaults to
    ``UNVERIFIED``, and the coordinator only accepts a ``PROVIDER_ISSUED``
    observation that it obtained itself from the injected
    ``ObservationProvider``. Observation providers should build instances
    through :meth:`issued` so the provenance cannot be forgotten; a
    hand-built observation can never satisfy the A5 gate.
    """

    resource_id: str = Field(min_length=1)
    resource_type: SWSResourceType
    facts: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime
    ambiguous: bool = False
    arn: str | None = Field(default=None, min_length=1)
    account_id: str | None = Field(default=None, pattern=r"^[0-9]{12}$")
    region: str | None = None
    provenance: ObservationProvenance = ObservationProvenance.UNVERIFIED

    @classmethod
    def issued(cls, **kwargs: Any) -> ResourceObservation:
        """Build a provider-issued observation (M10).

        This is the only supported way for an ``ObservationProvider`` to
        produce evidence the execution gate will accept. It stamps
        ``provenance=PROVIDER_ISSUED``; the coordinator still cross-checks
        the identity and freshness of whatever it receives, so provenance is
        necessary but never sufficient.
        """
        kwargs["provenance"] = ObservationProvenance.PROVIDER_ISSUED
        return cls(**kwargs)

    @field_validator("resource_type", mode="before")
    @classmethod
    def _normalize_resource_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("observed_at", mode="before")
    @classmethod
    def _require_aware_datetime(cls, value: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("observation timestamps must be timezone-aware")
        return value


class ExecutionRequest(BaseModel):
    """Everything the execution gate needs to evaluate one attempt (M9/M10).

    ``action_plan_id`` + ``resource_id`` + ``action`` name the attempt. The
    gate cross-checks the ``plan``, the ``decision`` that motivated it, the
    ``snapshot`` the decision was derived from, the GRANTED ``ticket`` when
    human approval is required, and an observation the coordinator obtains
    itself from the injected provider. Every context field is optional at the
    boundary so each refusal case can be exercised; the gate is what requires
    them.

    M10 boundary: ``fresh_observation`` is **not** an input the gate will
    accept. The field is retained only so a caller that supplies one is
    refused explicitly (``CALLER_SUPPLIED_OBSERVATION_REJECTED``) instead of
    having its evidence silently dropped. ``expected_poststate`` is likewise
    not a source of truth: the gate verifies the canonical, action-derived
    postcondition and only requires a supplied value to agree with it.
    """

    action_plan_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    action: PotentialAction
    execution_mode: ExecutionMode
    plan: ActionPlan | None = None
    decision: PolicyDecision | None = None
    snapshot: WorkspaceSnapshot | None = None
    ticket: ApprovalTicket | None = None
    fresh_observation: ResourceObservation | None = None
    expected_poststate: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action", "execution_mode", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class MutationAttempt(BaseModel):
    """Single, opaque result from crossing the mutation boundary (M9).

    Produced by an injected mutation handler. ``ambiguous`` means the
    attempt's own outcome is unknown (for example the call timed out after
    being dispatched); an ambiguous attempt is never reported as anything
    other than UNKNOWN. ``call_error`` means the underlying call failed
    deterministically. ``sanitized`` carries only safe, non-secret evidence. A
    ``None`` resolution yields the attempt outcome whenever the boundary is a
    dry-run or verification-only boundary.
    """

    ambiguous: bool = False
    call_error: bool = False
    sanitized: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """Comparison of expected post-state facts against observed facts (M9).

    ``status`` follows the verification taxonomy from constants.py:

    - SUCCESS: every expected fact is confirmed by the observation.
    - FAILED: at least one observed fact directly contradicts an expected
      fact.
    - PARTIALLY_VERIFIED: some expected facts are confirmed but others could
      not be observed (the verifier reports partial evidence honestly).
    - UNKNOWN: the observation was unavailable or ambiguous (including a
      timeout); an UNKNOWN is never upgraded to SUCCESS.

    ``details`` is a human-readable, claim-kind-marked description used by
    the audit POST record.
    """

    status: VerificationStatus
    expected_facts: dict[str, Any] = Field(default_factory=dict)
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    details: list[str] = Field(default_factory=list)
    observed_at: datetime | None = None


class ExecutionResult(BaseModel):
    """Final outcome of an execution attempt after the full gate (M9).

    ``outcome`` is the canonical ``ExecutionOutcome``. On refusal the outcome
    is REFUSED, ``refusal`` names the deterministic reason, and no attempt was
    recorded. ``NOT_EXECUTED`` is the honest M9 statement that the gate
    passed but no mutation implementation exists, so nothing was crossed.
    Otherwise the outcome reflects the post-attempt verification
    (VERIFIED_SUCCESS / PARTIALLY_VERIFIED / FAILED / UNKNOWN).
    """

    execution_id: str = Field(min_length=1)
    action_plan_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    action: PotentialAction
    execution_mode: ExecutionMode
    outcome: ExecutionOutcome
    refusal: RefusalReason | None = None
    refusal_detail: str = ""
    verification: VerificationStatus | None = None
    attempt_id: str | None = Field(default=None, min_length=1)
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    completed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    note: str = ""

    @field_validator("action", "execution_mode", "outcome", "refusal", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value