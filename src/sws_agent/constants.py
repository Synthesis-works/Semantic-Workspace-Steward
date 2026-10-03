"""Canonical SWS definitions.

Single source of truth for every important state, label, action, and
constant used across the project. Import these definitions everywhere;
never repeat string literals across modules.

Lesson from SMS applied from day one: do not let different names exist
for the same state. The SWS action vocabulary is defined once here and
imported wherever needed, including in documentation and tests.
"""

from __future__ import annotations

import enum
from typing import Final


class SWSResourceType(str, enum.Enum):
    """AWS resource types targeted by SWS in the current MVP scope.

    Scope is intentionally narrow: S3, EC2/EBS or Lambda, and Cost
    Explorer data. Other services are added only after a documented
    scope decision.
    """

    S3_BUCKET = "s3_bucket"
    EC2_INSTANCE = "ec2_instance"
    EBS_VOLUME = "ebs_volume"
    LAMBDA_FUNCTION = "lambda_function"
    COST_DATA = "cost_data"


class PotentialAction(str, enum.Enum):
    """SWS action vocabulary.

    Fresh vocabulary for AWS workspace stewardship. This is NOT a copy
    of any file-lifecycle state vocabulary (e.g. KEEP / ARCHIVE / TRASH
    / QUARANTINE). Each action is a concept; actual side-effecting
    execution requires a documented safety review and is added later.
    """

    LEAVE = "leave"
    """No action is needed for this resource."""

    FLAG_FOR_REVIEW = "flag_for_review"
    """Surface the resource for human attention without acting on it."""

    REQUEST_APPROVAL = "request_approval"
    """The situation warrants a human decision before anything proceeds."""

    STOP_RESOURCE = "stop_resource"
    """Stop a resource in a safe, reversible way where explicitly supported."""


class ExecutionMode(str, enum.Enum):
    """Modes controlling how much autonomy SWS is granted."""

    SAFE = "safe"
    """Every side-effecting action requires human approval."""

    REVIEW = "review"
    """Side-effecting actions are scoped and human-reviewed."""

    AUTONOMOUS = "autonomous"
    """Only explicitly designated low-risk actions may proceed without approval."""


class RiskLevel(str, enum.Enum):
    """Risk classification for actions and resources."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AuthorizationDecision(str, enum.Enum):
    """Outcome of the deterministic authorization gate."""

    AUTHORIZED = "authorized"
    PENDING_APPROVAL = "pending_approval"
    BLOCKED = "blocked"


class TraceStatus(str, enum.Enum):
    """Statuses of execution events in the audit trace."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WAITING = "waiting"


class TraceEventType(str, enum.Enum):
    """Categories of recorded execution events.

    Fresh SWS vocabulary describing AWS workspace stewardship work.
    Replaces SMS's stage vocabulary (DISCOVERY / AI_ANALYSIS / POLICY /
    HUMAN_APPROVAL) with stewardship-oriented event types.
    """

    REQUEST_RECEIVED = "request_received"
    INVENTORY_QUERY = "inventory_query"
    ANALYSIS = "analysis"
    POLICY_EVALUATION = "policy_evaluation"
    APPROVAL_FLOW = "approval_flow"
    ACTION_EXECUTION = "action_execution"
    VERIFICATION = "verification"
    AUDIT = "audit"


class EvidenceBasis(str, enum.Enum):
    """How a relationship or claim is supported.

    Deterministic evidence (API state facts) always takes precedence over
    semantic inference.
    """

    DETERMINISTIC = "deterministic"
    """Supported by authoritative, machine-readable AWS state."""

    INFERRED = "inferred"
    """Derived from semantic reasoning; never overrides deterministic evidence."""


class RelationshipType(str, enum.Enum):
    """Controlled relationship vocabulary for deterministic resource linking.

    Closed enum with controlled extensibility: a new relationship type is
    only added after a documented scope decision defining its predicate and
    evidence contract. Free-form relationship labels are rejected at the
    model boundary.
    """

    SAME_ACCOUNT = "same_account"
    """Both resources expose the same, known 12-digit AWS account ID."""

    SAME_REGION = "same_region"
    """Both resources are observed in the same, known AWS region."""

    SAME_OWNER_TAG = "same_owner_tag"
    """Both resources carry the same, known Owner tag value.

    This asserts exact tag-string equality only; it never proves the owning
    entity is identical."""


class ClaimKind(str, enum.Enum):
    """Epistemic classification of any statement SWS produces.

    Observed statements are recorded from authoritative sources, derived
    statements come from deterministic analysis, and interpreted statements
    are natural-language explanations from an optional LLM. Interpreted
    claims never override derived or observed claims.
    """

    OBSERVED = "observed"
    """Recorded fact from an authoritative, machine-readable source."""

    DERIVED = "derived"
    """Result of deterministic rule-based analysis over observed facts."""

    INTERPRETED = "interpreted"
    """Natural-language interpretation; may be absent when no LLM provider is configured."""


class ApprovalStatus(str, enum.Enum):
    """Lifecycle states of a human-approval ticket (M12).

    M12 replaces the M9 "exactly one transition out of PENDING" rule with an
    explicit legal-transition table. PENDING and GRANTED are the only
    non-terminal states; DENIED, EXPIRED, CONSUMED, and REVOKED are terminal
    and admit no outgoing transition.

        PENDING -> GRANTED | DENIED | EXPIRED | REVOKED
        GRANTED -> CONSUMED | EXPIRED | REVOKED

    Two changes are load-bearing and were identified as defects by the M12
    investigation:

    * CONSUMED is a first-class status. M9 modelled consumption as an
      orthogonal ``consumed`` boolean on a ticket that remained GRANTED, so a
      redeemed approval was indistinguishable from a live one by status alone
      and could not be enumerated or audited by state.
    * REVOKED is new. Nothing in M9 could withdraw an already-granted
      approval, and GRANTED previously never expired, so a grant was valid
      for the entire remaining life of the process.

    The legal-transition table itself lives in ``approval.py``; this enum is
    the closed vocabulary of states only.
    """

    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    REVOKED = "revoked"


class CollectionFailureCategory(str, enum.Enum):
    """Classification of a failed collection step on an inventory run.

    Collectors emit exactly one category on every FAILED inventory event
    (Option A from the M2C-B design review). The workspace builder trusts
    the collector's emission; if the metadata is ever absent it defensively
    falls back on whether the resource type also produced a SUCCEEDED event.
    """

    PRIMARY = "primary"
    """The primary list operation for a resource type failed; collection of
    that type is incomplete, which makes the snapshot ``partial``."""

    ENRICHMENT = "enrichment"
    """A resource record was preserved but one enrichment lookup (region or
    tags) failed for it; the resource itself is still reported."""

    PARSE = "parse"
    """A returned API item was skipped because it could not be parsed (for
    example a Lambda function with a missing or malformed ARN)."""


class Ec2InstanceState(str, enum.Enum):
    """The closed set of EC2 instance lifecycle states reported by AWS.

    EC2 exposes ``State.Name`` from ``ec2:DescribeInstances`` and
    ``DescribeInstanceStatus``. The service documents exactly these seven
    values, so SWS defines the vocabulary once here rather than repeating the
    string literals in the collector, the policy engine, and the tests.

    This enum is the *policy* vocabulary, not the collection vocabulary. The
    collector records whatever AWS returned byte-for-byte in
    ``ResourceRecord.state`` and never maps it through this enum, so a state
    AWS adds in the future is recorded honestly and then rejected by policy as
    unrecognized rather than being coerced into one of these members.

    The terminal states (``shutting-down`` / ``terminated``) are permanent:
    an instance that reaches them cannot be stopped, restarted, or reclaimed.
    That is the reason they are grouped as never-actionable rather than merely
    "already satisfied".
    """

    PENDING = "pending"
    """Launch requested; the instance has not yet reached ``running``."""

    RUNNING = "running"
    """The instance is running. ``ec2:StopInstances`` is valid and the stop is
    reversible with ``ec2:StartInstances``."""

    SHUTTING_DOWN = "shutting-down"
    """Terminal transition already in progress; the instance cannot be stopped."""

    TERMINATED = "terminated"
    """Terminal. The instance no longer exists as a running resource."""

    STOPPING = "stopping"
    """A stop already requested; the instance is converging on ``stopped``."""

    STOPPED = "stopped"
    """Not running, but the instance still exists and can be started again.
    The ``STOP_RESOURCE`` postcondition already holds."""


SWS_SUPPORTED_EC2_INSTANCE_STATES: Final[frozenset[str]] = frozenset(
    state.value for state in Ec2InstanceState
)
"""The complete set of canonical EC2 instance lifecycle states."""

EC2_STOP_ELIGIBLE_STATES: Final[frozenset[Ec2InstanceState]] = frozenset(
    {Ec2InstanceState.PENDING, Ec2InstanceState.RUNNING}
)
"""States from which ``ec2:StopInstances`` is valid and the stop is reversible.

``running`` is the ordinary case. ``pending`` is included because the instance
is still converging toward running and AWS accepts a stop for it; refusing to
stop a pending instance would leave the only state in which a stop is still
cheap and side-effect-free to act upon.

Deliberately excluded: ``stopping`` and ``stopped`` (the postcondition already
holds or is arriving, so a stop would be a redundant call), and the terminal
states (no stop is possible at all).
"""

EC2_STOP_INELIGIBLE_STATES: Final[frozenset[Ec2InstanceState]] = frozenset(
    {
        Ec2InstanceState.STOPPING,
        Ec2InstanceState.STOPPED,
        Ec2InstanceState.SHUTTING_DOWN,
        Ec2InstanceState.TERMINATED,
    }
)
"""States for which no stop may ever be recommended.

``STOP_RESOURCE`` is defined as a *reversible* stop, and this set is exactly
the states where nothing needs doing: the postcondition already holds, is
already arriving, or is unreachable because the instance is gone.
"""

OWNER_TAG_COLLECTED_RESOURCE_TYPES: Final[frozenset[SWSResourceType]] = frozenset(
    {SWSResourceType.S3_BUCKET, SWSResourceType.LAMBDA_FUNCTION}
)
"""Resource types whose collector actually reads the ``Owner`` tag.

This set exists so the deterministic policy engine can tell an *absent* Owner
tag (a fact about the resource) apart from a *never-collected* Owner tag (a
fact about SWS's own AWS surface). The distinction matters because the
owner-tag rules reason from absence: M13-A's EC2 collector is bound to the
single read-only ``ec2:DescribeInstances`` seam, which does not return tags,
so every EC2 instance necessarily arrives with ``owner_tag=None`` no matter
what the instance is tagged with. Treating that as a missing-tag finding would
report "unknown ownership" about every instance in the account while
establishing nothing.

A type appearing here is a statement that its collector performs a tag lookup
and traced a failure when that lookup failed. A type absent from this set has
no Owner-tag evidence, and no owner-tag rule may conclude anything from it.
"""


# ---------------------------------------------------------------------------
# Resource-specific limits.
# Lesson from SMS day one: do not reuse SMS's document-size limits for AWS
# resources, and do not invent arbitrary limits without documenting them.
# Each limit below states its purpose. Limits stay centralized here.
# ---------------------------------------------------------------------------

MAX_RESOURCES_PER_INVENTORY_REQUEST: Final[int] = 100
"""Upper bound on resources returned in a single inventory request.

Chosen to keep a single response bounded for a personal AWS account
while still being useful; larger inventories use pagination."""

MAX_RESOURCES_FOR_RELATIONSHIP_DERIVATION: Final[int] = 1000
"""Maximum number of resources a snapshot may contain for relationship derivation.

Pairwise comparison is O(n^2); this cap bounds CPU and output growth for
personal AWS workspaces while avoiding premature optimization. Snapshots
with more resources raise WorkspaceSnapshotTooLargeError."""

MAX_COST_WINDOW_DAYS: Final[int] = 92
"""Longest cost window (days) a caller may request from Cost Explorer.

Allows full-quarter analysis while staying within Cost Explorer's
daily-granularity history limit. Cost queries cannot exceed this."""

MAX_COST_GROUP_BY_KEYS: Final[int] = 4
"""Maximum number of group-by keys for a Cost Explorer query.

Hard ceiling to bound response size; the service itself caps grouping
well below this."""

MAX_COST_EXPLORER_PAGES: Final[int] = 20
"""Maximum Cost Explorer response pages the collector will follow.

``get_cost_and_usage`` can paginate via ``NextPageToken`` (each page is a
separate AWS request). This cap bounds latency and billed requests; when it
is reached with a token outstanding the collection is honestly marked
truncated rather than silently reported as complete."""

AWS_API_RETRY_ATTEMPTS: Final[int] = 5
"""Maximum retry attempts for AWS API calls.

Capped lower than the boto3 default to bound latency and retry cost."""

AWS_API_TIMEOUT_SECONDS: Final[int] = 30
"""Per-call timeout for AWS API calls."""

MAX_TRACE_EVENTS: Final[int] = 10_000
"""Maximum recorded trace events per session.

Prevents unbounded memory growth in the audit store."""


# ---------------------------------------------------------------------------
# Canonical vocabulary invariants (asserted by tests).
# ---------------------------------------------------------------------------

SMS_DEPRECATED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"keep", "archive", "trash", "quarantine"}
)
"""File-lifecycle states from the SMS reference domain.

Used by tests to guarantee the SWS action vocabulary never drifts into
renamed file-lifecycle states."""

SWS_SUPPORTED_ACTIONS: Final[frozenset[str]] = frozenset(
    action.value for action in PotentialAction
)
"""The complete set of canonical action names. Import and reuse; do not
redefine."""

SWS_SUPPORTED_RESOURCE_TYPES: Final[frozenset[str]] = frozenset(
    resource.value for resource in SWSResourceType
)
"""The complete set of canonical resource types."""

SWS_SUPPORTED_COLLECTION_FAILURE_CATEGORIES: Final[frozenset[str]] = frozenset(
    category.value for category in CollectionFailureCategory
)
"""The complete set of canonical collection-failure categories."""

SWS_SUPPORTED_RELATIONSHIP_TYPES: Final[frozenset[str]] = frozenset(
    relationship.value for relationship in RelationshipType
)
"""The complete set of canonical relationship types."""

# ---------------------------------------------------------------------------
# Deterministic policy rules (M2C-D).
# Stable identifiers attached to PolicyDecision.rule so every decision is
# self-describing in audit evidence. A new rule is only added after its
# predicate and outcome are documented, matching the canonical-label rule.
# ---------------------------------------------------------------------------

POLICY_RULE_MISSING_OWNER_TAG: Final[str] = "missing_owner_tag"
"""No Owner tag is recorded on the resource (data assumed complete)."""

POLICY_RULE_OWNER_UNVERIFIABLE: Final[str] = "owner_unverifiable"
"""Owner-tag absence cannot be treated as fact because the workspace
inventory is partial or truncated; the resource is flagged for review."""

POLICY_RULE_EC2_STOP_ELIGIBLE: Final[str] = "ec2_stop_eligible"
"""An EC2 instance was observed in a state from which ``ec2:StopInstances`` is
valid and reversible (``running`` / ``pending``), so ``STOP_RESOURCE`` is
recommended.

The rule consumes a *positive observed fact*: the state AWS reported for this
instance. It is therefore evaluated even under a partial or truncated snapshot,
because completeness undermines conclusions drawn from an absence, not from a
value the API actually returned. The snapshot is still required to have
reported the instance at all.
"""

POLICY_RULE_EC2_STOP_NOT_ELIGIBLE: Final[str] = "ec2_stop_not_eligible"
"""An EC2 instance was observed in a state where no stop may ever be
recommended (``stopping`` / ``stopped`` / ``shutting-down`` / ``terminated``):
the postcondition already holds or is arriving, or the instance is gone.

This is a positive determination, not an absence, so it is reported with full
confidence. ``LEAVE`` here states that SWS recommends nothing, never that the
resource needs nothing.
"""

POLICY_RULE_EC2_STATE_UNRECOGNIZED: Final[str] = "ec2_state_unrecognized"
"""An EC2 instance's recorded state is blank, absent, or a value outside the
documented EC2 state set, so SWS cannot establish what stopping it would mean.

The resource is flagged for review instead of being recommended for or
exempted from a stop. SWS never maps an unknown state onto a neighbouring
member of the enum, because a stop recommendation is only safe when it is
derived from a state AWS actually documented.
"""

SWS_SUPPORTED_POLICY_RULES: Final[frozenset[str]] = frozenset(
    {
        POLICY_RULE_MISSING_OWNER_TAG,
        POLICY_RULE_OWNER_UNVERIFIABLE,
        POLICY_RULE_EC2_STOP_ELIGIBLE,
        POLICY_RULE_EC2_STOP_NOT_ELIGIBLE,
        POLICY_RULE_EC2_STATE_UNRECOGNIZED,
    }
)
"""The complete set of canonical deterministic policy rule identifiers."""

# ---------------------------------------------------------------------------
# Cost collection (M2C-E) controlled group-by vocabulary.
# These are the only group-by keys the collector may request; anything else
# is rejected rather than silently translated into an unknown dimension.
# ---------------------------------------------------------------------------

COST_GROUP_DIMENSION_SERVICE: Final[str] = "service"
"""Group cost estimates by AWS service (Cost Explorer DIMENSION SERVICE)."""

COST_GROUP_TAG_OWNER: Final[str] = "owner_tag"
"""Group cost estimates by the ``Owner`` AWS tag value (Cost Explorer TAG)."""

SWS_SUPPORTED_COST_GROUP_BY_KEYS: Final[frozenset[str]] = frozenset(
    {COST_GROUP_DIMENSION_SERVICE, COST_GROUP_TAG_OWNER}
)
"""The complete set of cost group-by keys the collector may request."""

SWS_SUPPORTED_EXECUTION_MODES: Final[frozenset[str]] = frozenset(
    mode.value for mode in ExecutionMode
)
"""The complete set of canonical execution modes."""


class ExecutionStage(str, enum.Enum):
    """Phases of a gated execution attempt for the audit ledger.

    PRE marks the moment the gate finished evaluating and recorded intent;
    ATTEMPT marks the single moment a mutation boundary would be crossed;
    RESULT captures post-attempt verification; POST closes the record with
    the final outcome. A request that stops at PRE without an ATTEMPT is an
    honest statement that no mutation occurred.
    """

    PRE = "pre"
    ATTEMPT = "attempt"
    RESULT = "result"
    POST = "post"


class ExecutionOutcome(str, enum.Enum):
    """Final outcome of an execution request after full verification.

    VERIFIED_SUCCESS is only ever reported when an independent observation
    confirmed the expected post-state. FAILED means a confirmed deviation or
    a known call error. UNKNOWN means the attempt or its outcome could not be
    firmly established (for example a timeout). PARTIALLY_VERIFIED means some
    expected facts were confirmed but others could not be observed. REFUSED
    and NOT_EXECUTED are the honest outcomes for requests that never crossed
    the mutation boundary.
    """

    REFUSED = "refused"
    NOT_EXECUTED = "not_executed"
    VERIFIED_SUCCESS = "verified_success"
    PARTIALLY_VERIFIED = "partially_verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ObservationProvenance(str, enum.Enum):
    """Where a ``ResourceObservation`` came from (M10).

    M9 accepted any ``ResourceObservation`` a caller attached to the request,
    which meant a caller could assert arbitrary post-state facts and satisfy
    the A5 freshness gate without any independent read. M10 makes the
    coordinator obtain the observation itself from the injected
    ``ObservationProvider`` and requires the returned observation to be
    provider-issued. ``UNVERIFIED`` is the default so a hand-built
    observation can never satisfy a gate by accident.
    """

    PROVIDER_ISSUED = "provider_issued"
    UNVERIFIED = "unverified"


SWS_MAX_OBSERVATION_AGE_SECONDS: Final[int] = 300
"""Maximum age of an A5 preflight observation, in seconds.

An observation older than this cannot describe the current state of the
resource, even if it is newer than the snapshot it is checked against. The
coordinator accepts an override for tests and operators; the default is the
canonical value.
"""


class VerificationStatus(str, enum.Enum):
    """How well the post-attempt state matched the expected post-state.

    SUCCESS requires the observed facts to match every expected fact.
    FAILED requires a confirmed contradiction. UNKNOWN is used whenever an
    observation is unavailable, ambiguous (for example a timeout), or the
    verifier cannot establish the outcome; it is NEVER upgraded to a success
    claim. PARTIALLY_VERIFIED reports partial evidence honestly.
    """

    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"
    PARTIALLY_VERIFIED = "partially_verified"


class RefusalReason(str, enum.Enum):
    """Controlled reason vocabulary for gate-level execution refusals.

    Every refusal SWS emits maps to exactly one reason value so callers can
    act on it deterministically. The list is closed: a new reason is only
    added after its gate condition is documented.
    """

    MISSING_PLAN = "missing_plan"
    MISSING_DECISION = "missing_decision"
    MISSING_SNAPSHOT = "missing_snapshot"
    MODE_MISMATCH = "mode_mismatch"
    ACTION_NOT_EXECUTABLE = "action_not_executable"
    RESOURCE_TYPE_MISMATCH = "resource_type_mismatch"
    RESOURCE_NOT_IN_SNAPSHOT = "resource_not_in_snapshot"
    PARTIAL_SNAPSHOT = "partial_snapshot"
    TRUNCATED_SNAPSHOT = "truncated_snapshot"
    AUTONOMOUS_EXECUTION_UNSUPPORTED = "autonomous_execution_unsupported"
    MISSING_TICKET = "missing_ticket"
    TICKET_PENDING = "ticket_pending"
    TICKET_DENIED = "ticket_denied"
    TICKET_EXPIRED = "ticket_expired"
    TICKET_MISMATCH_ACTION = "ticket_mismatch_action"
    TICKET_MISMATCH_RESOURCE = "ticket_mismatch_resource"
    TICKET_MISMATCH_PLAN = "ticket_mismatch_plan"
    TICKET_CONSUMED = "ticket_consumed"
    # M12: a granted approval can now be withdrawn before it is redeemed.
    # This is a distinct refusal from TICKET_DENIED because a DENIED ticket
    # was never approved while a REVOKED ticket was approved and later
    # withdrawn; collapsing them would misreport what the human did.
    TICKET_REVOKED = "ticket_revoked"
    DECISION_MISMATCH = "decision_mismatch"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    RESOURCE_IDENTITY_MISMATCH = "resource_identity_mismatch"
    STALE_OBSERVATION = "stale_observation"
    ALREADY_EXECUTED = "already_executed"
    DUPLICATE_ATTEMPT = "duplicate_attempt"
    # M13 Phase 4: a reservation conflict is not a duplicate *attempt*. It says
    # another worker already owns this exact (intent_key, ticket_id) pair, so the
    # authorization instance named here has been claimed. DUPLICATE_ATTEMPT stays
    # reserved for the plan/intent level checks that predate the execution ledger,
    # because a reservation conflict is a statement about who holds the claim
    # rather than about how many attempts were observed.
    EXECUTION_ALREADY_RESERVED = "execution_already_reserved"
    # The ticket moved between the reservation and the consumption CAS. The
    # coordinator binds the exact revision it reserved and will not re-read or
    # manufacture a current one, so this is a refusal and not a retry: the ledger
    # row it just wrote stays in place for an operator.
    TICKET_REVISION_MISMATCH = "ticket_revision_mismatch"
    EXECUTION_NOT_IMPLEMENTED = "execution_not_implemented"
    # M10: preflight-evidence hardening. A5 evidence must be issued by the
    # injected observation provider, must be temporally bounded, must carry
    # complete identity, and must verify an action-derived postcondition.
    OBSERVATION_PROVIDER_UNAVAILABLE = "observation_provider_unavailable"
    OBSERVATION_NOT_PROVIDER_ISSUED = "observation_not_provider_issued"
    CALLER_SUPPLIED_OBSERVATION_REJECTED = "caller_supplied_observation_rejected"
    SNAPSHOT_FRESHNESS_UNESTABLISHED = "snapshot_freshness_unestablished"
    OBSERVATION_FROM_FUTURE = "observation_from_future"
    OBSERVATION_TOO_OLD = "observation_too_old"
    IDENTITY_EVIDENCE_MISSING = "identity_evidence_missing"
    POSTCONDITION_UNDEFINED = "postcondition_undefined"
    POSTCONDITION_MISMATCH = "postcondition_mismatch"
    DURABLE_LEDGER_REQUIRED = "durable_ledger_required"


SWS_SUPPORTED_REFUSAL_REASONS: Final[frozenset[str]] = frozenset(
    reason.value for reason in RefusalReason
)
"""The complete set of canonical gate-refusal reasons."""

SWS_SUPPORTED_EXECUTION_STAGES: Final[frozenset[str]] = frozenset(
    stage.value for stage in ExecutionStage
)
"""The complete set of canonical execution stages."""

SWS_SUPPORTED_EXECUTION_OUTCOMES: Final[frozenset[str]] = frozenset(
    outcome.value for outcome in ExecutionOutcome
)
"""The complete set of canonical execution outcomes."""

SWS_SUPPORTED_VERIFICATION_STATUSES: Final[frozenset[str]] = frozenset(
    status.value for status in VerificationStatus
)
"""The complete set of canonical verification statuses."""

SWS_SUPPORTED_OBSERVATION_PROVENANCES: Final[frozenset[str]] = frozenset(
    provenance.value for provenance in ObservationProvenance
)
"""The complete set of canonical observation-provenance values."""