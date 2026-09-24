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
    """Lifecycle states of a human-approval ticket.

    Tickets enter the store as PENDING and transition exactly once to
    GRANTED, DENIED, or EXPIRED; no other transition is valid.
    """

    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


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

SWS_SUPPORTED_POLICY_RULES: Final[frozenset[str]] = frozenset(
    {POLICY_RULE_MISSING_OWNER_TAG, POLICY_RULE_OWNER_UNVERIFIABLE}
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