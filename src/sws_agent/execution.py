"""Gated execution boundary and the guarded mutation registry (M9, hardened in M10).

M9 built the execution-safety machinery while deliberately keeping the
production system unable to mutate AWS. M10 hardens the *evidence* that
machinery trusts, without adding any AWS surface and without registering a
handler:

  - **A5 evidence is provider-issued.** The coordinator no longer reads an
    observation from the request. It calls the injected ``ObservationProvider``
    itself and requires a ``provider_issued`` observation whose identity and
    freshness it then re-checks. A caller-supplied observation is refused
    outright (``CALLER_SUPPLIED_OBSERVATION_REJECTED``) rather than being
    trusted or silently dropped.
  - **Freshness is bounded on both sides.** The observation must be newer than
    ``snapshot.collected_at`` (not merely the snapshot's start time), not
    dated in the future, and not older than a maximum age. A snapshot with no
    ``collected_at`` cannot establish freshness and is refused.
  - **Identity is mandatory.** resource id, type, ARN, account and region must
    all be present on both the snapshot record and the observation and must
    agree. A missing identity fact is a refusal, never a skipped comparison.
  - **Postconditions are action-derived.** What "success" means comes from
    ``ActionSpec.postconditions`` via :func:`canonical_postconditions`. A caller
    cannot choose the expectation; a supplied value must agree with the
    canonical one or the request is refused.
  - **Idempotency is keyed on a durable intent.** :func:`execution_intent_key`
    derives a deterministic digest from ``(snapshot_id, resource_id, action)``
    so a re-planned identical intent is recognised as a duplicate across
    process restarts. A handler may not be used without a durable ledger.
  - **Observation failures cannot escape.** Any provider failure after the
    attempt -- including an unexpected exception -- becomes ``UNKNOWN`` and
    still writes RESULT and POST, so the ledger never keeps a silently open
    transaction. :func:`reconcile_open_transactions` reads such transactions
    back without repairing them.

Unchanged from M9: ``ACTION_EXECUTION_REGISTRY`` is metadata only (every entry
``implemented=False``, ``mutation`` a documentation string, no callables), no
handler is registered, ``interfaces.ActionExecutor`` is not used, and
``ExecutionCoordinator`` refuses autonomous execution of approval-required
actions. Production therefore still cannot mutate anything.

Boundaries: this module never imports the AWS SDK, never constructs an AWS
API client, never calls arbitrary AWS methods, and never enables
autonomous execution of approval-required actions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Mapping, Protocol, runtime_checkable
from uuid import uuid4

from ._identity import utc_now
from .approval import UnknownTicketError
from .audit import AuditRecordKind, AuditStore, execution_payload
from .constants import (
    SWS_MAX_OBSERVATION_AGE_SECONDS,
    ApprovalStatus,
    ExecutionMode,
    ExecutionOutcome,
    ExecutionStage,
    ObservationProvenance,
    PotentialAction,
    RefusalReason,
    SWSResourceType,
    VerificationStatus,
)
from .interfaces import ApprovalStore
from .models import (
    ExecutionRequest,
    ExecutionResult,
    MutationAttempt,
    ResourceObservation,
    VerificationResult,
    WorkspaceSnapshot,
)
from .verification import (
    DefaultOutcomeVerifier,
    ObservationError,
    ObservationProvider,
    OutcomeVerifier,
)


_TICKET_STATE_REFUSALS: dict[
    ApprovalStatus, tuple[RefusalReason, str]
] = {
    ApprovalStatus.PENDING: (
        RefusalReason.TICKET_PENDING,
        "attached ticket is still pending approval",
    ),
    ApprovalStatus.DENIED: (
        RefusalReason.TICKET_DENIED,
        "attached ticket was denied",
    ),
    ApprovalStatus.EXPIRED: (
        RefusalReason.TICKET_EXPIRED,
        "attached ticket has expired",
    ),
    ApprovalStatus.CONSUMED: (
        RefusalReason.TICKET_CONSUMED,
        "ticket was already consumed by another attempt",
    ),
    ApprovalStatus.REVOKED: (
        RefusalReason.TICKET_REVOKED,
        "attached ticket was revoked after approval",
    ),
}
"""M12: refusal for every approval state that cannot authorize an attempt.

GRANTED is deliberately absent: it is the only state that reaches the binding
checks in :meth:`ExecutionCoordinator._ticket_gate`. DENIED and REVOKED are
kept distinct because a DENIED ticket was never approved while a REVOKED one
was approved and later withdrawn.
"""

_UNMAPPED_TICKET_STATES: frozenset[ApprovalStatus] = (
    frozenset(ApprovalStatus)
    - frozenset(_TICKET_STATE_REFUSALS)
    - {ApprovalStatus.GRANTED}
)
if _UNMAPPED_TICKET_STATES:  # pragma: no cover - import-time invariant
    # Fail fast rather than at the first execution attempt. An approval state
    # with no refusal entry would make the ticket gate treat it as GRANTED and
    # pass it, which is fail-open on the one gate that stands between a stored
    # string and a real mutation.
    raise RuntimeError(
        "every non-GRANTED ApprovalStatus needs a ticket-gate refusal; "
        f"unmapped: {sorted(s.value for s in _UNMAPPED_TICKET_STATES)}"
    )


@dataclass(frozen=True)
class ActionSpec:
    """Static description of one action's execution contract.

    ``mutation`` is documentation only (a plain string naming the future
    AWS operation, for example ``"ec2:StopInstances"``). It is never a
    callable and never a live client reference; ``implemented=False`` in M9
    means no code exists anywhere that could perform this mutation.

    ``postconditions`` (M10) is the canonical, deterministic statement of what
    must be true after the action succeeds. It is declared per action and read
    only through :func:`canonical_postconditions`; a caller can never choose
    what gets verified. An action that is not executable declares an empty
    mapping, which the gate refuses with ``POSTCONDITION_UNDEFINED`` so a
    non-executable action can never gain a verifiable success path.
    """

    action: PotentialAction
    eligible_resource_types: frozenset[SWSResourceType]
    requires_human_approval: bool
    mutation: str | None
    postconditions: Mapping[str, Any]
    implemented: bool = False
    description: str = ""


_ACTION_EXECUTION_REGISTRY_SOURCE: dict[PotentialAction, ActionSpec] = {
    PotentialAction.LEAVE: ActionSpec(
        action=PotentialAction.LEAVE,
        eligible_resource_types=frozenset(),
        requires_human_approval=False,
        mutation=None,
        postconditions=MappingProxyType({}),
        implemented=False,
        description=(
            "LEAVE is a no-op by definition and has no execution boundary "
            "to cross."
        ),
    ),
    PotentialAction.FLAG_FOR_REVIEW: ActionSpec(
        action=PotentialAction.FLAG_FOR_REVIEW,
        eligible_resource_types=frozenset(),
        requires_human_approval=False,
        mutation=None,
        postconditions=MappingProxyType({}),
        implemented=False,
        description=(
            "FLAG_FOR_REVIEW surfaces a resource for human attention; it "
            "has no execution boundary to cross."
        ),
    ),
    PotentialAction.REQUEST_APPROVAL: ActionSpec(
        action=PotentialAction.REQUEST_APPROVAL,
        eligible_resource_types=frozenset(),
        requires_human_approval=False,
        mutation=None,
        postconditions=MappingProxyType({}),
        implemented=False,
        description=(
            "REQUEST_APPROVAL opens the human approval flow; there is "
            "nothing to execute."
        ),
    ),
    PotentialAction.STOP_RESOURCE: ActionSpec(
        action=PotentialAction.STOP_RESOURCE,
        eligible_resource_types=frozenset({SWSResourceType.EC2_INSTANCE}),
        requires_human_approval=True,
        mutation=(
            "ec2:StopInstances (documented future boundary only; no "
            "mutation handler is registered in M9)"
        ),
        # Canonical postcondition metadata only. Nothing in SWS produces
        # these facts today: there is no EC2 read-back primitive and no
        # handler, so this declaration can never be satisfied in production.
        postconditions=MappingProxyType({"state": "stopped"}),
        implemented=False,
        description=(
            "STOP_RESOURCE targets EC2 instances and requires human "
            "approval, decision/snapshot consistency, a fresh provider-issued "
            "observation, and a registered mutation handler. M10 registers "
            "no handler."
        ),
    ),
}

ACTION_EXECUTION_REGISTRY: Final[Mapping[PotentialAction, ActionSpec]] = (
    MappingProxyType(_ACTION_EXECUTION_REGISTRY_SOURCE)
)
"""Immutable registry: metadata only, no callables, nothing executable."""


def canonical_postconditions(action: PotentialAction) -> Mapping[str, Any]:
    """Return the canonical post-state facts for ``action`` (M10).

    This is the single source of truth for what "success" means for an
    action. The coordinator verifies the returned mapping and never a
    caller-supplied one, so a caller cannot pick a trivially satisfiable
    expectation and obtain ``VERIFIED_SUCCESS``. An action with no declared
    postcondition returns an empty mapping, which the gate refuses.
    """
    spec = ACTION_EXECUTION_REGISTRY.get(action)
    if spec is None:
        return MappingProxyType({})
    return spec.postconditions


def execution_intent_key(
    *, snapshot_id: str, resource_id: str, action: PotentialAction
) -> str:
    """Return the deterministic durable intent key for one execution intent.

    M9 keyed duplicate detection on ``action_plan_id``, which is a fresh uuid
    every time a plan is created. Re-planning the same intent after a restart
    therefore produced a new id and evaded the durable duplicate scan, so an
    ambiguous attempt could be replayed.

    The key is a SHA-256 digest over a canonical JSON encoding of
    ``(snapshot_id, resource_id, action)`` -- stable inputs only. It never
    depends on the ticket id, the plan id, or any random value, so the same
    intent always yields the same key. It is snapshot-scoped by design: a new
    snapshot legitimately re-establishes the world and may produce a new
    intent. The derivation is stable enough to recompute from a stored audit
    record (``snapshot_id`` + ``resource_id`` + ``payload["action"]``), which
    is how the duplicate scan and the reconciliation reader work without any
    ledger schema change.
    """
    encoded = json.dumps(
        [snapshot_id, resource_id, action.value],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@runtime_checkable
class MutationHandler(Protocol):
    """The single seam where a mutation could occur.

    Production registers no handler in M9. A handler receives the fully
    gated ``ExecutionRequest`` and returns an opaque ``MutationAttempt``;
    it must never raise to report a normal outcome (an exception is treated
    as an ambiguous, unknown attempt).
    """

    def handle(self, request: ExecutionRequest) -> MutationAttempt: ...


class ExecutionCoordinator:
    """Deterministic gate keeper for one execution attempt (M9, hardened in M10).

    The coordinator is read-only over the plan, decision, snapshot, and
    ticket it is handed; it never mutates AWS and never mutates those
    objects. It owns the execution attempt's idempotency (in-memory plus a
    durable duplicate scan over prior EXECUTION records, keyed on the
    deterministic intent key) and, on gated execution, consumes the granted
    ticket so it can never authorize a second attempt.

    M10 evidence rules: the A5 observation is obtained from the injected
    provider rather than the request, postconditions are action-derived, and
    identity plus freshness are mandatory on both sides.

    M12: the approval dependency is the ``interfaces.ApprovalStore``
    protocol rather than a concrete class, so a durable implementation can
    replace the in-memory test double with no change here. The ticket gate
    now maps every non-GRANTED state to its own refusal; previously an
    unrecognized state fell through to "expired", and consumption was
    detected from an orthogonal boolean rather than from status.

    Refusals write no audit record: SWS never records a false durable claim,
    and a refused request performs no AWS call.
    """

    def __init__(
        self,
        *,
        approval_store: ApprovalStore,
        handler: MutationHandler | None = None,
        observer: ObservationProvider | None = None,
        verifier: OutcomeVerifier | None = None,
        audit_store: AuditStore | None = None,
        id_source: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
        max_observation_age_seconds: int = SWS_MAX_OBSERVATION_AGE_SECONDS,
    ) -> None:
        self._store = approval_store
        self._handler = handler
        self._observer = observer
        self._verifier = (
            verifier if verifier is not None else DefaultOutcomeVerifier()
        )
        self._audit_store = audit_store
        self._id_source = id_source or (lambda: uuid4().hex)
        self._now = now or utc_now
        self._max_observation_age = timedelta(
            seconds=max_observation_age_seconds
        )
        self._attempted: set[str] = set()
        self._intents: set[str] = set()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Run the gate and, if it passes, cross the boundary exactly once."""
        started_at = self._now()
        execution_id = self._id_source()
        gate = self._gate(request)
        if gate is not None:
            reason, detail = gate
            return ExecutionResult(
                execution_id=execution_id,
                action_plan_id=request.action_plan_id,
                resource_id=request.resource_id,
                action=request.action,
                execution_mode=request.execution_mode,
                outcome=ExecutionOutcome.REFUSED,
                refusal=reason,
                refusal_detail=detail,
                started_at=started_at,
                completed_at=self._now(),
            )

        spec = ACTION_EXECUTION_REGISTRY[request.action]
        intent_key = self._intent_key(request)
        self._attempted.add(request.action_plan_id)
        self._intents.add(intent_key)
        self._write(
            stage=ExecutionStage.PRE,
            request=request,
            execution_id=execution_id,
            note="gate passed; intent recorded",
        )

        # M9/M10 honesty boundary: no mutation handler is registered, so even a
        # fully gated request must report NOT_EXECUTED, never a fabricated
        # success.
        if self._handler is None:
            return ExecutionResult(
                execution_id=execution_id,
                action_plan_id=request.action_plan_id,
                resource_id=request.resource_id,
                action=request.action,
                execution_mode=request.execution_mode,
                outcome=ExecutionOutcome.NOT_EXECUTED,
                started_at=started_at,
                completed_at=self._now(),
                note=(
                    "no mutation handler is registered; the gate passed "
                    "but nothing was executed"
                ),
            )

        attempt: MutationAttempt | None = None
        handler_note = ""
        try:
            attempt = self._handler.handle(request)
        except Exception as exc:  # noqa: BLE001 - boundary never fabricates success
            handler_note = (
                f"mutation handler raised {type(exc).__name__}; "
                "outcome cannot be established"
            )
        self._write(
            stage=ExecutionStage.ATTEMPT,
            request=request,
            execution_id=execution_id,
            attempt=attempt,
            note=(
                handler_note
                or (
                    "mutation boundary crossed"
                    if attempt is not None and not attempt.ambiguous
                    else "mutation boundary reached"
                )
            ),
        )

        verification_status: VerificationStatus
        verification_record: VerificationResult | None = None
        expected = canonical_postconditions(request.action)
        if attempt is not None and attempt.ambiguous:
            verification_status = VerificationStatus.UNKNOWN
            verification_record = VerificationResult(
                status=VerificationStatus.UNKNOWN,
                details=["attempt outcome is ambiguous; post-state cannot be verified"],
            )
        elif attempt is not None and attempt.call_error:
            verification_status = VerificationStatus.FAILED
            verification_record = VerificationResult(
                status=VerificationStatus.FAILED,
                details=["mutation call reported an error"],
            )
        elif handler_note:
            verification_status = VerificationStatus.UNKNOWN
            verification_record = VerificationResult(
                status=VerificationStatus.UNKNOWN,
                details=[handler_note],
            )
        else:
            # M10: the canonical, action-derived expectation is verified -- not
            # whatever the caller asked for. Any observation problem, including
            # an unexpected provider exception, becomes UNKNOWN so RESULT and
            # POST are always written and the ledger is never left open.
            observation_note = self._post_attempt_observe(request)
            if isinstance(observation_note, str):
                verification_status = VerificationStatus.UNKNOWN
                verification_record = VerificationResult(
                    status=VerificationStatus.UNKNOWN,
                    details=[observation_note],
                )
            else:
                try:
                    verification_record = self._verifier.verify(
                        observation=observation_note,
                        expected_facts=dict(expected),
                    )
                except Exception as exc:  # noqa: BLE001 - never fabricates success
                    verification_status = VerificationStatus.UNKNOWN
                    verification_record = VerificationResult(
                        status=VerificationStatus.UNKNOWN,
                        details=[
                            "verifier raised "
                            f"{type(exc).__name__}; outcome cannot be established"
                        ],
                    )
                else:
                    verification_status = verification_record.status
        self._write(
            stage=ExecutionStage.RESULT,
            request=request,
            execution_id=execution_id,
            attempt=attempt,
            verification=verification_record,
        )

        outcome = _outcome_for_status(verification_status)
        note_parts = [handler_note or ""] + list(
            verification_record.details if verification_record is not None else []
        )
        consumed = False
        if spec.requires_human_approval and request.ticket is not None:
            try:
                self._store.consume(request.ticket.ticket_id)
                consumed = True
            except Exception as exc:  # noqa: BLE001 - reported, never fabricated
                note_parts.append(
                    f"ticket {request.ticket.ticket_id} could not be "
                    f"consumed: {type(exc).__name__}"
                )
        self._write(
            stage=ExecutionStage.POST,
            request=request,
            execution_id=execution_id,
            outcome=outcome,
            verification=None,
            attempt=attempt,
            consumed=consumed,
            note="; ".join(part for part in note_parts if part),
        )
        return ExecutionResult(
            execution_id=execution_id,
            action_plan_id=request.action_plan_id,
            resource_id=request.resource_id,
            action=request.action,
            execution_mode=request.execution_mode,
            outcome=outcome,
            verification=verification_status,
            attempt_id=execution_id,
            started_at=started_at,
            completed_at=self._now(),
            note="; ".join(part for part in note_parts if part),
        )

    def _post_attempt_observe(
        self, request: ExecutionRequest
    ) -> ResourceObservation | str:
        """Observe the post-attempt state, or explain why it is unusable.

        Returns a ``ResourceObservation`` only when the provider returned a
        provider-issued, unambiguous observation of the same resource.
        Otherwise returns a short sanitized reason string (never an exception
        message or traceback). Every failure mode is UNKNOWN by construction.
        """
        if self._observer is None:
            return "no observation provider is configured"
        try:
            observation = self._observer.observe(request.resource_id)
        except ObservationError:
            return "observation provider could not establish an authoritative state"
        except Exception as exc:  # noqa: BLE001 - a provider must never break the ledger
            return (
                "observation provider raised "
                f"{type(exc).__name__}; outcome cannot be established"
            )
        if observation is None:
            return "no observation available; post-state cannot be verified"
        if not isinstance(observation, ResourceObservation):
            return (
                "observation provider returned "
                f"{type(observation).__name__} instead of a ResourceObservation"
            )
        if observation.provenance is not ObservationProvenance.PROVIDER_ISSUED:
            return "post-attempt observation is not provider-issued"
        if observation.ambiguous:
            return "post-attempt observation is ambiguous; post-state cannot be verified"
        if observation.resource_id != request.resource_id:
            return "post-attempt observation targets a different resource"
        return observation

    def _intent_key(self, request: ExecutionRequest) -> str:
        snapshot = request.snapshot
        return execution_intent_key(
            snapshot_id=snapshot.snapshot_id if snapshot is not None else "",
            resource_id=request.resource_id,
            action=request.action,
        )

    def _preflight_gate(
        self, request: ExecutionRequest
    ) -> tuple[RefusalReason, str] | None:
        """A5: obtain and validate provider-issued preflight evidence."""
        if request.fresh_observation is not None:
            return (
                RefusalReason.CALLER_SUPPLIED_OBSERVATION_REJECTED,
                "caller-supplied observations cannot satisfy the freshness "
                "gate; the coordinator obtains its own evidence",
            )
        if self._observer is None:
            return (
                RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE,
                "no observation provider is configured, so freshness "
                "cannot be established",
            )
        try:
            observation = self._observer.observe(request.resource_id)
        except ObservationError:
            return (
                RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE,
                "observation provider could not establish an authoritative "
                "state",
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on any provider failure
            return (
                RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE,
                f"observation provider raised {type(exc).__name__}; "
                "freshness cannot be established",
            )
        if observation is None:
            return (
                RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE,
                "observation provider returned no observation",
            )
        if not isinstance(observation, ResourceObservation):
            return (
                RefusalReason.OBSERVATION_PROVIDER_UNAVAILABLE,
                "observation provider returned "
                f"{type(observation).__name__} instead of a ResourceObservation",
            )
        if observation.provenance is not ObservationProvenance.PROVIDER_ISSUED:
            return (
                RefusalReason.OBSERVATION_NOT_PROVIDER_ISSUED,
                "observation provenance is "
                f"{observation.provenance.value}; provider-issued evidence "
                "is required",
            )
        if observation.ambiguous:
            return (
                RefusalReason.STALE_OBSERVATION,
                "observation is ambiguous; the current state cannot be "
                "established",
            )
        return self._freshness_gate(request, observation)

    def _freshness_gate(
        self, request: ExecutionRequest, observation: ResourceObservation
    ) -> tuple[RefusalReason, str] | None:
        snapshot = request.snapshot
        assert snapshot is not None  # guaranteed by the caller
        now = self._now()

        if snapshot.collected_at is None:
            return (
                RefusalReason.SNAPSHOT_FRESHNESS_UNESTABLISHED,
                "snapshot has no collected_at timestamp, so no observation "
                "can be shown to post-date the inventory it relies on",
            )
        if observation.observed_at > now:
            return (
                RefusalReason.OBSERVATION_FROM_FUTURE,
                "observation is dated after the current time and cannot be "
                "trusted",
            )
        if observation.observed_at < snapshot.collected_at:
            return (
                RefusalReason.STALE_OBSERVATION,
                "observation predates the snapshot's collection, so it does "
                "not describe the state the decision was made against",
            )
        if now - observation.observed_at > self._max_observation_age:
            return (
                RefusalReason.OBSERVATION_TOO_OLD,
                "observation is older than the maximum permitted age of "
                f"{int(self._max_observation_age.total_seconds())}s",
            )
        return self._identity_gate(request, observation)

    @staticmethod
    def _identity_gate(
        request: ExecutionRequest, observation: ResourceObservation
    ) -> tuple[RefusalReason, str] | None:
        """A1 on evidence: complete identity, compared on both sides.

        A missing identity fact is a refusal. M9 skipped the comparison when
        either side was ``None``, which meant a record without an ARN silently
        disabled the ARN check.
        """
        resource = ExecutionCoordinator._find_resource(
            request.snapshot, request.resource_id
        )
        if resource is None:  # pragma: no cover - checked earlier in _gate
            return (
                RefusalReason.RESOURCE_NOT_IN_SNAPSHOT,
                "target resource is not present in the snapshot",
            )

        if observation.resource_id != request.resource_id:
            return (
                RefusalReason.RESOURCE_IDENTITY_MISMATCH,
                "observation targets a different resource",
            )
        if observation.resource_type is not resource.resource_type:
            return (
                RefusalReason.RESOURCE_IDENTITY_MISMATCH,
                "observation reports a different resource type",
            )
        identity_facts = (
            ("arn", resource.arn, observation.arn),
            ("account_id", resource.account_id, observation.account_id),
            ("region", resource.region, observation.region),
        )
        for name, expected_value, observed_value in identity_facts:
            if expected_value is None or observed_value is None:
                return (
                    RefusalReason.IDENTITY_EVIDENCE_MISSING,
                    f"{name} is missing from the snapshot record or the "
                    "observation; target identity cannot be established",
                )
            if expected_value != observed_value:
                return (
                    RefusalReason.RESOURCE_IDENTITY_MISMATCH,
                    f"observation {name} does not match the snapshot record",
                )
        return None

    def _postcondition_gate(
        self, request: ExecutionRequest
    ) -> tuple[RefusalReason, str] | None:
        """The expectation is action-derived; a supplied value must agree."""
        canonical = canonical_postconditions(request.action)
        if not canonical:
            return (
                RefusalReason.POSTCONDITION_UNDEFINED,
                f"{request.action.value} declares no canonical postcondition, "
                "so no execution could ever be verified",
            )
        supplied = dict(request.expected_poststate)
        if supplied and supplied != dict(canonical):
            return (
                RefusalReason.POSTCONDITION_MISMATCH,
                "the supplied expected post-state does not match the "
                "canonical postcondition for this action",
            )
        return None

    def _gate(
        self, request: ExecutionRequest
    ) -> tuple[RefusalReason, str] | None:
        plan = request.plan
        if plan is None:
            return RefusalReason.MISSING_PLAN, "request carries no plan"
        if plan.action_plan_id != request.action_plan_id:
            return (
                RefusalReason.MISSING_PLAN,
                "plan action_plan_id does not match the request",
            )
        if request.decision is None:
            return RefusalReason.MISSING_DECISION, "request carries no decision"
        if request.snapshot is None:
            return RefusalReason.MISSING_SNAPSHOT, "request carries no snapshot"
        if request.execution_mode is not plan.execution_mode:
            return (
                RefusalReason.MODE_MISMATCH,
                "request execution_mode does not match the plan execution_mode",
            )

        spec = ACTION_EXECUTION_REGISTRY.get(request.action)
        if spec is None or not spec.eligible_resource_types:
            return (
                RefusalReason.ACTION_NOT_EXECUTABLE,
                f"{request.action.value} has no eligible resource types",
            )

        # M10: crossing the mutation boundary without a durable ledger would
        # make the intent key unenforceable across restarts, so a handler is
        # only usable together with an audit store.
        if self._handler is not None and self._audit_store is None:
            return (
                RefusalReason.DURABLE_LEDGER_REQUIRED,
                "a registered mutation handler requires a durable audit "
                "store so the intent key can be enforced across restarts",
            )

        resource = self._find_resource(request.snapshot, request.resource_id)
        if resource is None:
            return (
                RefusalReason.RESOURCE_NOT_IN_SNAPSHOT,
                "target resource is not present in the snapshot",
            )
        if resource.resource_type not in spec.eligible_resource_types:
            return (
                RefusalReason.RESOURCE_TYPE_MISMATCH,
                f"resource type {resource.resource_type.value} is not "
                f"eligible for {request.action.value}",
            )

        if request.snapshot.partial:
            return (
                RefusalReason.PARTIAL_SNAPSHOT,
                "snapshot is partial; target state cannot be trusted",
            )
        if request.snapshot.truncated:
            return (
                RefusalReason.TRUNCATED_SNAPSHOT,
                "snapshot is truncated; target state cannot be trusted",
            )

        if spec.requires_human_approval:
            if request.execution_mode is ExecutionMode.AUTONOMOUS:
                return (
                    RefusalReason.AUTONOMOUS_EXECUTION_UNSUPPORTED,
                    "SWS does not enable autonomous execution of "
                    "approval-required actions",
                )
            gate = self._ticket_gate(request)
            if gate is not None:
                return gate

        gate = self._decision_gate(request)
        if gate is not None:
            return gate

        gate = self._postcondition_gate(request)
        if gate is not None:
            return gate

        if plan.executed:
            return (
                RefusalReason.ALREADY_EXECUTED,
                "plan is already marked executed",
            )
        if (
            request.action_plan_id in self._attempted
            or self._durable_attempt_exists(request.action_plan_id)
        ):
            return (
                RefusalReason.DUPLICATE_ATTEMPT,
                "an execution attempt already exists for this plan",
            )
        intent_key = self._intent_key(request)
        if intent_key in self._intents or self._durable_intent_exists(intent_key):
            return (
                RefusalReason.DUPLICATE_ATTEMPT,
                "an execution attempt already exists for this intent "
                "(snapshot, resource, and action)",
            )

        # M10: A5 evidence is required exactly when a mutation is possible.
        # With no handler registered the request cannot reach the mutation
        # boundary at all, so it resolves to NOT_EXECUTED below and no
        # pre-mutation observation is consumed on its behalf.
        if self._handler is None:
            return None
        return self._preflight_gate(request)

    def _ticket_gate(
        self, request: ExecutionRequest
    ) -> tuple[RefusalReason, str] | None:
        """Refuse unless the attached ticket is a live, bound, unexpired GRANT.

        M12: every non-GRANTED state maps to a distinct refusal. The M9 gate
        tested three states and used "expired" as a catch-all, which would
        have misreported a CONSUMED or REVOKED ticket as expired; it also
        detected consumption from the orthogonal ``consumed`` boolean, so a
        CONSUMED ticket was checked as if it were merely GRANTED and relied
        on a later boolean test to reject it. Status is now the single
        authority.
        """
        ticket = request.ticket
        if ticket is None:
            return (
                RefusalReason.MISSING_TICKET,
                "approval is required but no ticket is attached",
            )
        try:
            stored = self._store.get(ticket.ticket_id)
        except UnknownTicketError:
            return (
                RefusalReason.MISSING_TICKET,
                "attached ticket is not present in the approval store",
            )
        refusal = (
            None
            if stored.status is ApprovalStatus.GRANTED
            else _TICKET_STATE_REFUSALS[stored.status]
        )
        if refusal is not None:
            return refusal
        if stored.resource_id != request.resource_id:
            return (
                RefusalReason.TICKET_MISMATCH_RESOURCE,
                "ticket approves a different resource",
            )
        if stored.action is not request.action:
            return (
                RefusalReason.TICKET_MISMATCH_ACTION,
                "ticket approves a different action",
            )
        if stored.plan_id is not request.action_plan_id:
            return (
                RefusalReason.TICKET_MISMATCH_PLAN,
                "ticket is not bound to this plan",
            )
        return None

    def _decision_gate(
        self, request: ExecutionRequest
    ) -> tuple[RefusalReason, str] | None:
        decision = request.decision
        if decision.resource_id != request.resource_id:
            return (
                RefusalReason.DECISION_MISMATCH,
                "decision targets a different resource",
            )
        if decision.recommended_action is not request.action:
            return (
                RefusalReason.DECISION_MISMATCH,
                "decision recommends a different action",
            )
        if (
            request.plan.decision_id is not None
            and request.plan.decision_id != decision.decision_id
        ):
            return (
                RefusalReason.DECISION_MISMATCH,
                "plan is stamped with a different decision",
            )
        if (
            decision.snapshot_id is not None
            and decision.snapshot_id != request.snapshot.snapshot_id
        ):
            return (
                RefusalReason.SNAPSHOT_MISMATCH,
                "decision is not derived from the attached snapshot",
            )
        if (
            decision.run_id is not None
            and request.snapshot.run_id is not None
            and decision.run_id != request.snapshot.run_id
        ):
            return (
                RefusalReason.SNAPSHOT_MISMATCH,
                "decision run does not match the attached snapshot run",
            )
        return None

    def _durable_attempt_exists(self, action_plan_id: str) -> bool:
        if self._audit_store is None:
            return False
        for envelope in self._audit_store.records():
            if envelope.kind is not AuditRecordKind.EXECUTION:
                continue
            if envelope.action_plan_id != action_plan_id:
                continue
            stage = (envelope.payload or {}).get("stage")
            if stage in ("attempt", "result", "post"):
                return True
        return False

    def _durable_intent_exists(self, intent_key: str) -> bool:
        """True when the ledger already holds an attempt for this intent.

        M10: the scan keys on the deterministic intent key, which is
        recomputed from each stored record's ``snapshot_id`` +
        ``resource_id`` + ``payload["action"]``. A re-planned identical intent
        (new ``action_plan_id``, same snapshot/resource/action) is therefore
        still recognised, including after a process restart. No ledger schema
        change is involved.
        """
        if self._audit_store is None:
            return False
        for envelope in self._audit_store.records():
            if envelope.kind is not AuditRecordKind.EXECUTION:
                continue
            payload = envelope.payload or {}
            if payload.get("stage") not in ("attempt", "result", "post"):
                continue
            if _intent_key_of(envelope) != intent_key:
                continue
            return True
        return False

    def _write(
        self,
        *,
        stage: ExecutionStage,
        request: ExecutionRequest,
        execution_id: str,
        outcome: ExecutionOutcome | None = None,
        attempt: MutationAttempt | None = None,
        verification: Any | None = None,
        consumed: bool = False,
        note: str = "",
    ) -> None:
        if self._audit_store is None:
            return
        snapshot = request.snapshot
        decision = request.decision
        self._audit_store.write(
            kind=AuditRecordKind.EXECUTION,
            run_id=snapshot.run_id if snapshot is not None else None,
            snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
            decision_id=decision.decision_id if decision is not None else None,
            action_plan_id=request.action_plan_id,
            ticket_id=(
                request.ticket.ticket_id if request.ticket is not None else None
            ),
            resource_id=request.resource_id,
            payload=execution_payload(
                stage=stage,
                execution_id=execution_id,
                request=request,
                outcome=outcome,
                attempt=attempt,
                verification=verification,
                consumed=consumed,
                note=note,
            ),
        )

    @staticmethod
    def _find_resource(
        snapshot: WorkspaceSnapshot, resource_id: str
    ):
        for record in snapshot.resources:
            if record.resource_id == resource_id:
                return record
        return None


# ---------------------------------------------------------------------------
# Open-transaction reconciliation (M10, read-only).
# ---------------------------------------------------------------------------

_OPENING_STAGES: Final[tuple[str, ...]] = ("attempt", "result")
_CLOSING_STAGE: Final[str] = "post"


def _intent_key_of(envelope: Any) -> str | None:
    """Recompute the intent key of a stored EXECUTION record, if derivable.

    The ledger deliberately stores the stable inputs rather than the derived
    key, so the key is recomputed with the same canonical helper. Returns
    ``None`` when a required input is missing (for example a record written by
    an older version), which callers must treat as "not comparable" rather
    than as a match.
    """
    snapshot_id = envelope.snapshot_id
    resource_id = envelope.resource_id
    action = (envelope.payload or {}).get("action")
    if not snapshot_id or not resource_id or not action:
        return None
    try:
        return execution_intent_key(
            snapshot_id=snapshot_id,
            resource_id=resource_id,
            action=PotentialAction(action),
        )
    except ValueError:
        return None


@dataclass(frozen=True)
class OpenTransaction:
    """One execution transaction that never reached its POST record (M10).

    An open transaction means the process crossed (or may have crossed) the
    mutation boundary and then stopped: the durable ledger holds a PRE and at
    least one ATTEMPT/RESULT record but no closing POST. Its real outcome is
    therefore *not known* -- it is neither a success nor a failure, and SWS
    never guesses. The record exists so an operator can find such a
    transaction deterministically; resolving it is deliberately out of scope.
    """

    execution_id: str
    action_plan_id: str | None
    intent_key: str | None
    resource_id: str | None
    action: PotentialAction | None
    last_stage: ExecutionStage
    stages: tuple[ExecutionStage, ...]
    classification: str = "unresolved_open_transaction"


def reconcile_open_transactions(audit_store: AuditStore) -> tuple[OpenTransaction, ...]:
    """Return every execution transaction in the ledger that never closed.

    Read-only by construction: this inspects the ledger and returns a
    deterministic, ``execution_id``-ordered tuple. It never writes a record,
    never repairs a transaction, never consumes a ticket, never retries an
    attempt, and never executes anything.

    A transaction counts as open when it has at least one ATTEMPT or RESULT
    record and no POST record. A gate-passing request that ended
    ``NOT_EXECUTED`` wrote only PRE and is therefore *not* open: nothing was
    attempted, so there is nothing to resolve.
    """
    grouped: dict[str, list[Any]] = {}
    for envelope in audit_store.records():
        if envelope.kind is not AuditRecordKind.EXECUTION:
            continue
        payload = envelope.payload or {}
        execution_id = payload.get("execution_id")
        stage = payload.get("stage")
        if not execution_id or stage is None:
            continue
        grouped.setdefault(execution_id, []).append((envelope, stage))

    open_transactions: list[OpenTransaction] = []
    for execution_id in sorted(grouped):
        entries = grouped[execution_id]
        stages = tuple(
            ExecutionStage(stage) for _envelope, stage in entries
        )
        if _CLOSING_STAGE in (stage.value for stage in stages):
            continue
        if not any(stage.value in _OPENING_STAGES for stage in stages):
            continue
        first_envelope = entries[0][0]
        ordered = sorted(
            entries,
            key=lambda item: (
                item[0].created_at is None,
                item[0].created_at or datetime.min.replace(tzinfo=None),
                item[1],
            ),
        )
        last_stage = ExecutionStage(ordered[-1][1])
        action_raw = (first_envelope.payload or {}).get("action")
        try:
            action = PotentialAction(action_raw) if action_raw else None
        except ValueError:
            action = None
        open_transactions.append(
            OpenTransaction(
                execution_id=execution_id,
                action_plan_id=first_envelope.action_plan_id,
                intent_key=_intent_key_of(first_envelope),
                resource_id=first_envelope.resource_id,
                action=action,
                last_stage=last_stage,
                stages=tuple(sorted(stages, key=lambda stage: stage.value)),
            )
        )
    return tuple(open_transactions)


def _outcome_for_status(
    status: VerificationStatus | None,
) -> ExecutionOutcome:
    if status is VerificationStatus.SUCCESS:
        return ExecutionOutcome.VERIFIED_SUCCESS
    if status is VerificationStatus.FAILED:
        return ExecutionOutcome.FAILED
    if status is VerificationStatus.PARTIALLY_VERIFIED:
        return ExecutionOutcome.PARTIALLY_VERIFIED
    return ExecutionOutcome.UNKNOWN