"""The live ``ec2:StopInstances`` composition root and its two orchestration steps.

This module assembles the *same* production classes ``build_dry_run_composition``
assembles -- :class:`DurableApprovalStore`, :class:`DurableExecutionLedger`,
:class:`Ec2InstanceObservationProvider`, :class:`ExecutionCoordinator` -- but the
difference is the handler and the client it is given:

* the dry-run root refuses any handler that can reach an environment and only
  ever accepts a read-only EC2 seam;
* this root builds the *real* mutation client (``MutationClientFactory``) with
  explicitly-passed instance-profile credentials, arms the single-dispatch
  witness on it, and injects a real :class:`StopInstancesHandler`.

There is no dry-run flag and no branch: the coordinator cannot tell the two
apart, which is the point. Safety comes from *what the handler is*, and here it
is a handler that can reach AWS, deliberately, for one pre-authorized instance.

Two invariants this module is built to hold:

* ``STOP_RESOURCE.implemented`` stays ``False``. The coordinator's gate never
  consults that flag, so the live path does not need it set, and
  ``preflight.check_program_invariants`` *refuses* if any action is marked
  implemented. Flipping it would both be unnecessary and break the program
  invariant, so this module verifies the flag is still ``False`` before it will
  assemble anything.
* A dispatch happens at most once. The client is built with
  ``total_max_attempts=1`` and the handler re-checks that; the
  :class:`DispatchWitness` is armed on the client so a second invocation would be
  evidence, not an assumption.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from sws_agent._identity import utc_now
from sws_agent.approval_ledger import DurableApprovalStore
from sws_agent.approval_signature import (
    ApprovalFields,
    SignedApproval,
    parse_public_key_raw,
    verify_signed_approval,
)
from sws_agent.audit import LEDGER_FILENAME, JsonlAuditStore
from sws_agent.composition import (
    APPROVAL_LEDGER_FILENAME,
    EXECUTION_LEDGER_FILENAME,
    build_approval_verifier,
)
from sws_agent.constants import (
    AuthorizationDecision,
    ExecutionMode,
    PotentialAction,
    RiskLevel,
    SWSResourceType,
)
from sws_agent.ec2_mutation import StopInstancesHandler
from sws_agent.ec2_mutation_client import (
    MutationClientFactory,
    MutationClientSettings,
    MutationCredentials,
)
from sws_agent.ec2_observation import (
    DEFAULT_PARTITION,
    Ec2InstanceObservationProvider,
)
from sws_agent.execution import (
    ACTION_EXECUTION_REGISTRY,
    ExecutionCoordinator,
    execution_intent_key,
)
from sws_agent.execution_ledger import DurableExecutionLedger
from sws_agent.models import (
    ActionPlan,
    AuthorizationResult,
    ExecutionRequest,
    ExecutionResult,
    PolicyDecision,
    ResourceRecord,
    WorkspaceSnapshot,
)
from sws_agent.mutation_evidence import DispatchWitness

DEFAULT_DEADLINE_MINUTES: Final[int] = 30
"""Default signed execution window, well inside the store's 1-hour ceiling."""

STOP_RESOURCE_ACTION_VALUE: Final[str] = PotentialAction.STOP_RESOURCE.value
"""The exact action string the approver signs (``"stop_resource"``)."""

_INSTANCE_ARN_RE: Final[re.Pattern[str]] = re.compile(
    r"^arn:(?P<partition>[^:]+):ec2:(?P<region>[^:]+):(?P<account>\d{12})"
    r":instance/(?P<instance>i-[0-9a-fA-F]+)$"
)


class LiveStopError(RuntimeError):
    """A live-stop step was refused before anything was constructed."""


@dataclass(frozen=True)
class ParsedInstanceArn:
    """The identity components of a single EC2 instance ARN."""

    partition: str
    region: str
    account_id: str
    instance_id: str


def parse_instance_arn(arn: str) -> ParsedInstanceArn:
    """Parse one EC2 instance ARN, refusing anything that is not exactly one."""
    if not isinstance(arn, str):
        raise LiveStopError(f"target_instance_arn must be a string, got {type(arn).__name__}")
    match = _INSTANCE_ARN_RE.match(arn.strip())
    if match is None:
        raise LiveStopError(
            "target_instance_arn must be a single EC2 instance ARN of the form "
            f"arn:<partition>:ec2:<region>:<account>:instance/<id>, got {arn!r}"
        )
    return ParsedInstanceArn(
        partition=match.group("partition"),
        region=match.group("region"),
        account_id=match.group("account"),
        instance_id=match.group("instance"),
    )


def _assert_environment_consistent(
    parsed: ParsedInstanceArn, *, region: str, account_id: str, partition: str
) -> None:
    """Refuse a configured environment that disagrees with the target ARN.

    All three values are pinned both in the ARN and in the operator's
    configuration. Comparing them here turns a paste error into a refusal at the
    start of the run rather than a mismatched approval at the gate.
    """
    for name, configured, from_arn in (
        ("region", region, parsed.region),
        ("account_id", account_id, parsed.account_id),
        ("partition", partition, parsed.partition),
    ):
        if configured != from_arn:
            raise LiveStopError(
                f"configured {name}={configured!r} does not match the target ARN's "
                f"{name}={from_arn!r}; the executor environment and the mutation "
                "target must agree"
            )


def read_instance_profile_credentials(
    session_factory: Any = None,
) -> MutationCredentials:
    """Read the ambient instance-profile credentials and freeze them explicitly.

    boto3 is imported lazily so this composition root can be imported in a
    hermetic test environment without the SDK. The frozen credentials are passed
    to :class:`MutationClientSettings` explicitly; the factory never consults the
    ambient chain itself, so the only place the chain is read is here, visibly.
    """
    if session_factory is None:
        import boto3  # type: ignore[import-untyped]  # lazy: SDK stays out of import

        session_factory = boto3.session.Session
    session = session_factory()
    credentials = session.get_credentials()
    if credentials is None:
        raise LiveStopError(
            "no credentials are available from the instance profile; the live "
            "stop requires the executor's instance-profile credentials"
        )
    frozen = credentials.get_frozen_credentials()
    if not frozen.access_key or not frozen.secret_key:
        raise LiveStopError(
            "the instance profile returned incomplete credentials; refusing to "
            "build a mutation client that might fall back to the ambient chain"
        )
    return MutationCredentials(
        access_key_id=frozen.access_key,
        secret_access_key=frozen.secret_key,
        session_token=frozen.token,
        source="instance-profile",
    )


class Ec2ReadSeam:
    """Narrow the mutation client to the single read the observer needs.

    :class:`Ec2InstanceObservationProvider` requires a seam exposing
    ``describe_instances()`` that *returns the paginator*. A raw boto3 client
    exposes ``get_paginator("describe_instances")`` instead, so this adapter
    supplies exactly the one method the provider calls and nothing else.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def describe_instances(self) -> Any:
        return self._client.get_paginator("describe_instances")


@dataclass(frozen=True)
class LiveStopComposition:
    """Everything one live stop needs, plus the handle to release it."""

    coordinator: ExecutionCoordinator
    approval_store: DurableApprovalStore
    execution_ledger: DurableExecutionLedger
    audit_store: JsonlAuditStore | None
    observer: Ec2InstanceObservationProvider
    handler: StopInstancesHandler
    client: Any
    witness: DispatchWitness | None
    target_instance_id: str

    def close(self) -> None:
        """Release every store this composition opened, best effort."""
        for closeable in (
            self.audit_store,
            self.execution_ledger,
            self.approval_store,
        ):
            if closeable is None:
                continue
            try:
                closeable.close()
            except Exception:  # noqa: BLE001,S110 - teardown must not mask cause
                pass

    def assert_single_dispatch(self) -> None:
        """Raise unless exactly one dispatch reached the boundary.

        Called after an execution that reached the handler. The witness is the
        authority: it observes botocore's own attempt events, so it cannot be
        fooled by a handler that merely *claims* to have sent one request.
        """
        if len(self.handler.dispatch_calls) > 1:
            raise LiveStopError(
                f"the handler dispatched {len(self.handler.dispatch_calls)} times; "
                "the single-dispatch invariant is violated"
            )
        if self.witness is not None and self.handler.dispatch_calls:
            self.witness.assert_single_dispatch()


def _assert_program_invariants() -> None:
    """Refuse to assemble a live root if any action is marked implemented.

    The live path is an overlay: it injects a real handler into an otherwise
    unmodified coordinator. If some action were marked implemented, the program
    invariant that ``preflight`` checks would already be broken, so assembling a
    live run on top of a changed registry would be building on drift. Fail closed.
    """
    implemented = sorted(
        action.value
        for action, spec in ACTION_EXECUTION_REGISTRY.items()
        if spec.implemented
    )
    if implemented:
        raise LiveStopError(
            "refusing to assemble a live stop: actions are marked implemented "
            f"({implemented}); the live path must enable dispatch by injecting a "
            "handler, never by flipping a registry flag"
        )


def build_live_stop_composition(
    *,
    ledger_dir: str | Path,
    target_instance_arn: str,
    region: str,
    executor_instance_id: str,
    account_id: str,
    approval_signer_public_keys: Mapping[str, bytes],
    operator_identities: frozenset[str] = frozenset(),
    partition: str = DEFAULT_PARTITION,
    credentials: MutationCredentials | None = None,
    client: Any = None,
    session_factory: Any = None,
    arm_witness: bool = True,
    audit: bool = True,
    now: Callable[[], datetime] | None = None,
    worker_id: str | None = None,
    sleep: Callable[[float], None] | None = None,
    max_observation_age_seconds: int | None = None,
) -> LiveStopComposition:
    """Assemble the real coordinator over real durable stores and a live client.

    ``client`` and ``credentials`` exist so the composition is constructible in a
    hermetic test with a hand-built client. In production both are omitted: the
    credentials are read once from the instance profile and the client is built
    by the one factory SWS permits to build a mutating client.
    """
    if not approval_signer_public_keys:
        raise LiveStopError(
            "a live stop requires at least one pinned approver public key; "
            "without one the detached-approval gate cannot be armed"
        )
    if not executor_instance_id or not account_id:
        raise LiveStopError(
            "executor_instance_id and account_id are required: a live approval is "
            "bound to the exact host and account it authorizes"
        )
    parsed = parse_instance_arn(target_instance_arn)
    _assert_environment_consistent(
        parsed, region=region, account_id=account_id, partition=partition
    )
    _assert_program_invariants()

    if client is None:
        resolved_credentials = credentials or read_instance_profile_credentials(
            session_factory=session_factory
        )
        settings = MutationClientSettings(
            region=region,
            credentials=resolved_credentials,
            target_instance_arn=target_instance_arn,
        )
        client = MutationClientFactory(settings, session_factory=session_factory)()

    witness: DispatchWitness | None = None
    if arm_witness:
        witness = DispatchWitness()
        witness.arm(client)

    handler = StopInstancesHandler(client, region=region)
    observer = Ec2InstanceObservationProvider(
        Ec2ReadSeam(client),
        region=region,
        partition=partition,
        now=now,
    )

    root = Path(ledger_dir)
    approval_store = DurableApprovalStore(
        root / APPROVAL_LEDGER_FILENAME, now=now
    )
    try:
        execution_ledger = DurableExecutionLedger(
            root / EXECUTION_LEDGER_FILENAME, now=now
        )
        try:
            audit_store = (
                JsonlAuditStore(root / LEDGER_FILENAME, now=now) if audit else None
            )
            try:
                coordinator = ExecutionCoordinator(
                    approval_store=approval_store,
                    execution_ledger=execution_ledger,
                    handler=handler,
                    observer=observer,
                    audit_store=audit_store,
                    worker_id=worker_id,
                    now=now,
                    sleep=sleep,
                    max_observation_age_seconds=(
                        max_observation_age_seconds
                        if max_observation_age_seconds is not None
                        else 300
                    ),
                    approval_verifier=build_approval_verifier(
                        public_keys=approval_signer_public_keys,
                        executor_instance_id=executor_instance_id,
                        account_id=account_id,
                        region=region,
                        operator_identities=operator_identities,
                        now=now,
                        nonce_lookup=approval_store.lookup_nonce,
                    ),
                )
            except BaseException:
                if audit_store is not None:
                    audit_store.close()
                raise
        except BaseException:
            execution_ledger.close()
            raise
    except BaseException:
        approval_store.close()
        raise

    return LiveStopComposition(
        coordinator=coordinator,
        approval_store=approval_store,
        execution_ledger=execution_ledger,
        audit_store=audit_store,
        observer=observer,
        handler=handler,
        client=client,
        witness=witness,
        target_instance_id=parsed.instance_id,
    )


def _resolve_clock(now: Callable[[], datetime] | None) -> Callable[[], datetime]:
    return now or utc_now


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise LiveStopError(f"{field} is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise LiveStopError(f"{field} must be timezone-aware: {value!r}")
    return parsed.astimezone(timezone.utc)


def prepare_live_stop(
    *,
    ledger_dir: str | Path,
    target_instance_arn: str,
    region: str,
    executor_instance_id: str,
    account_id: str,
    snapshot_id: str,
    partition: str = DEFAULT_PARTITION,
    plan_id: str | None = None,
    nonce: str | None = None,
    rationale: str = "",
    deadline_minutes: int = DEFAULT_DEADLINE_MINUTES,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Mint a pending ticket and return the signing request for the approver.

    This step performs **no** AWS call and does not need credentials. It records
    a durable PENDING ticket bound to the plan and intent key, then renders the
    exact request the approver signs. The intent key is derived from the
    snapshot id, so it is fixed here and re-derived (and checked) at execute time.
    """
    if not snapshot_id or not snapshot_id.strip():
        raise LiveStopError("snapshot_id is required to bind the execution intent")
    if not 1 <= deadline_minutes <= 60:
        raise LiveStopError(
            "deadline_minutes must be between 1 and 60; the durable store's "
            "execution ceiling is one hour, and a longer signed window would be "
            "refused at grant time"
        )
    parsed = parse_instance_arn(target_instance_arn)
    _assert_environment_consistent(
        parsed, region=region, account_id=account_id, partition=partition
    )
    clock = _resolve_clock(now)

    resolved_plan_id = plan_id or uuid4().hex
    resolved_nonce = nonce or uuid4().hex
    intent_key = execution_intent_key(
        snapshot_id=snapshot_id,
        resource_id=parsed.instance_id,
        action=PotentialAction.STOP_RESOURCE,
    )
    deadline = clock() + timedelta(minutes=deadline_minutes)

    root = Path(ledger_dir)
    store = DurableApprovalStore(root / APPROVAL_LEDGER_FILENAME, now=now)
    try:
        ticket = store.create_ticket(
            resource_id=parsed.instance_id,
            action=PotentialAction.STOP_RESOURCE,
            rationale=rationale,
            ticket_id=f"live-stop-{resolved_nonce}",
            plan_id=resolved_plan_id,
            execution_intent_key=intent_key,
        )
        ticket_id = ticket.ticket_id
    finally:
        store.close()

    return {
        "ticket_id": ticket_id,
        "resource_id": parsed.instance_id,
        "action": STOP_RESOURCE_ACTION_VALUE,
        "plan_id": resolved_plan_id,
        "execution_intent_key": intent_key,
        "executor_instance_id": executor_instance_id,
        "account_id": account_id,
        "region": region,
        "nonce": resolved_nonce,
        "execution_deadline": deadline.isoformat(),
        "snapshot_id": snapshot_id,
    }


def parse_signed_approval_artifact(artifact: Mapping[str, Any]) -> SignedApproval:
    """Rebuild the typed :class:`SignedApproval` from the transport JSON.

    The JSON is pure transport; the only thing that carries authority is the
    Ed25519 signature over the canonical encoding of these fields, which the
    execution gate re-derives from the *stored* ticket. This function only
    reconstructs the artifact the approver produced.
    """
    fields = artifact.get("fields")
    signature = artifact.get("signature")
    if not isinstance(fields, Mapping) or not isinstance(signature, str) or not signature.strip():
        raise LiveStopError(
            "the signed approval artifact must carry a 'fields' object and a "
            "non-blank 'signature'"
        )
    required = (
        "ticket_id",
        "resource_id",
        "action",
        "executor_instance_id",
        "account_id",
        "region",
        "nonce",
        "execution_deadline",
        "issued_at",
        "signer_key_id",
    )
    missing = [name for name in required if not fields.get(name)]
    if missing:
        raise LiveStopError(f"signed approval artifact is missing fields: {missing}")
    return SignedApproval(
        fields=ApprovalFields(
            ticket_id=fields["ticket_id"],
            resource_id=fields["resource_id"],
            action=fields["action"],
            plan_id=fields.get("plan_id"),
            execution_intent_key=fields.get("execution_intent_key"),
            execution_deadline=_parse_timestamp(
                fields["execution_deadline"], "execution_deadline"
            ),
            executor_instance_id=fields["executor_instance_id"],
            account_id=fields["account_id"],
            region=fields["region"],
            nonce=fields["nonce"],
            issued_at=_parse_timestamp(fields["issued_at"], "issued_at"),
            signer_key_id=fields["signer_key_id"],
        ),
        signature=signature,
    )


def _build_request(
    *,
    signed: SignedApproval,
    snapshot_id: str,
    target_instance_arn: str,
    region: str,
    account_id: str,
    activity_at: datetime,
) -> ExecutionRequest:
    """Assemble the exact request the coordinator's gate is written to accept.

    Every cross-reference the gate checks is made consistent here in one place:
    ``plan.action_plan_id`` equals ``request.action_plan_id`` and the ticket's
    plan id; ``plan.decision_id`` equals ``decision.decision_id``;
    ``decision.snapshot_id`` equals the snapshot's id; and the snapshot record's
    identity equals the target ARN. The observation the gate requires is *not*
    supplied -- the coordinator obtains it from the observer, as A5 requires.

    The ``AUTHORIZED`` authorization decision and ``PolicyDecision`` are
    synthesized here **intentionally**. The live path's authority is the detached,
    signature-bound approval -- not a fresh call to the policy engine -- and these
    objects exist only to express the cross-references the gate re-checks. The
    synthesized result is therefore *not* an independent authorization bypass: the
    gate still verifies the signed approval (via ``approval_verifier``) and still
    enforces the ticket, reservation, and post-state rules.
    """
    fields = signed.fields
    if fields.plan_id is None:
        raise LiveStopError(
            "the signed approval carries no plan_id; a durable ticket must be "
            "bound to the plan it approves"
        )
    parsed = parse_instance_arn(target_instance_arn)
    resource_id = parsed.instance_id
    plan_id = fields.plan_id
    decision_id = f"decision:{plan_id}"
    record = ResourceRecord(
        resource_id=resource_id,
        resource_type=SWSResourceType.EC2_INSTANCE,
        region=region,
        state="running",
        arn=target_instance_arn,
        account_id=account_id,
    )
    snapshot = WorkspaceSnapshot(
        snapshot_id=snapshot_id,
        created_at=activity_at,
        collected_at=activity_at,
        regions=[region],
        resource_types=[SWSResourceType.EC2_INSTANCE],
        resources=[record],
        counts={SWSResourceType.EC2_INSTANCE: 1},
    )
    authorization = AuthorizationResult(
        decision=AuthorizationDecision.AUTHORIZED,
        reason="detached approval granted for one pre-authorized instance",
        requires_human_approval=True,
    )
    plan = ActionPlan(
        resource_id=resource_id,
        action=PotentialAction.STOP_RESOURCE,
        execution_mode=ExecutionMode.SAFE,
        authorization=authorization,
        action_plan_id=plan_id,
        created_at=activity_at,
        decision_id=decision_id,
        snapshot_id=snapshot_id,
    )
    decision = PolicyDecision(
        resource_id=resource_id,
        recommended_action=PotentialAction.STOP_RESOURCE,
        risk_level=RiskLevel.LOW,
        rationale="first authorized live stop",
        needs_approval=True,
        decision_id=decision_id,
        snapshot_id=snapshot_id,
    )
    return ExecutionRequest(
        action_plan_id=plan_id,
        resource_id=resource_id,
        action=PotentialAction.STOP_RESOURCE,
        execution_mode=ExecutionMode.SAFE,
        plan=plan,
        decision=decision,
        snapshot=snapshot,
    )


def execute_live_stop(
    *,
    ledger_dir: str | Path,
    target_instance_arn: str,
    region: str,
    executor_instance_id: str,
    account_id: str,
    snapshot_id: str,
    signed_approval: SignedApproval,
    approval_signer_public_keys: Mapping[str, bytes],
    operator_identities: frozenset[str] = frozenset(),
    partition: str = DEFAULT_PARTITION,
    credentials: MutationCredentials | None = None,
    client: Any = None,
    session_factory: Any = None,
    arm_witness: bool = True,
    audit: bool = True,
    now: Callable[[], datetime] | None = None,
    worker_id: str | None = None,
    sleep: Callable[[float], None] | None = None,
    decided_by: str = "approver",
) -> ExecutionResult:
    """Ingest a signed artifact, grant the ticket, and cross the boundary once.

    The order is deliberate: the artifact is validated and the signature is
    checked *before* the durable store is mutated, so a malformed or wrong-key
    artifact never leaves a GRANTED ticket behind. The execution gate repeats the
    signature check against the stored fields, so this early check is
    defence-in-depth rather than the authority.
    """
    signed = signed_approval
    fields = signed.fields
    parsed = parse_instance_arn(target_instance_arn)
    _assert_environment_consistent(
        parsed, region=region, account_id=account_id, partition=partition
    )
    if fields.action != STOP_RESOURCE_ACTION_VALUE:
        raise LiveStopError(
            f"the signed approval is for action {fields.action!r}, not "
            f"{STOP_RESOURCE_ACTION_VALUE!r}"
        )
    if fields.resource_id != parsed.instance_id:
        raise LiveStopError(
            f"the signed approval binds resource {fields.resource_id!r}, not the "
            f"target {parsed.instance_id!r}"
        )
    if fields.executor_instance_id != executor_instance_id:
        raise LiveStopError(
            "the signed approval was issued for executor "
            f"{fields.executor_instance_id!r}, not this host {executor_instance_id!r}"
        )
    if fields.account_id != account_id or fields.region != region:
        raise LiveStopError(
            "the signed approval's account/region do not match this executor"
        )

    expected_intent = execution_intent_key(
        snapshot_id=snapshot_id,
        resource_id=parsed.instance_id,
        action=PotentialAction.STOP_RESOURCE,
    )
    if fields.execution_intent_key != expected_intent:
        raise LiveStopError(
            "the signed approval's execution_intent_key does not match the "
            f"snapshot id {snapshot_id!r} and target; refusing to execute an "
            "approval that is not bound to this snapshot"
        )

    pinned = approval_signer_public_keys.get(fields.signer_key_id)
    if pinned is None:
        raise LiveStopError(
            f"the approval was signed by {fields.signer_key_id!r}, which is not "
            "among the pinned approver keys"
        )
    # Early tamper check (the gate re-checks): decode the signature over the
    # canonical encoding of the supplied fields. A failure here is a refusal
    # before any durable state is touched, so it is reported in this module's
    # own vocabulary rather than leaking the crypto layer's exception.
    try:
        verify_signed_approval(signed, parse_public_key_raw(pinned))
    except Exception as exc:  # noqa: BLE001 - any crypto failure is a refusal
        raise LiveStopError(
            f"the signed approval did not verify against the pinned key: "
            f"{type(exc).__name__}"
        ) from exc

    composition = build_live_stop_composition(
        ledger_dir=ledger_dir,
        target_instance_arn=target_instance_arn,
        region=region,
        executor_instance_id=executor_instance_id,
        account_id=account_id,
        approval_signer_public_keys=approval_signer_public_keys,
        operator_identities=operator_identities,
        partition=partition,
        credentials=credentials,
        client=client,
        session_factory=session_factory,
        arm_witness=arm_witness,
        audit=audit,
        now=now,
        worker_id=worker_id,
        sleep=sleep,
    )
    try:
        store = composition.approval_store
        try:
            ticket = store.get(fields.ticket_id)
        except Exception as exc:  # noqa: BLE001 - reported, never fabricated
            raise LiveStopError(
                f"no durable ticket {fields.ticket_id!r} to grant: {type(exc).__name__}"
            ) from exc
        if ticket.status.value == "pending":
            store.grant(fields.ticket_id, decided_by=decided_by, signed=signed)
        elif not (
            ticket.status.value == "granted" and ticket.nonce == fields.nonce
        ):
            raise LiveStopError(
                f"ticket {fields.ticket_id!r} is {ticket.status.value!r}; a live "
                "stop requires a pending ticket (or one already granted by this "
                "exact artifact)"
            )

        activity_at = _resolve_clock(now)()
        request = _build_request(
            signed=signed,
            snapshot_id=snapshot_id,
            target_instance_arn=target_instance_arn,
            region=region,
            account_id=account_id,
            activity_at=activity_at,
        )
        # Attach the ticket freshly read *after* the grant so it is GRANTED.
        request = request.model_copy(update={"ticket": store.get(fields.ticket_id)})

        result = composition.coordinator.execute(request)
        if composition.handler.dispatch_calls:
            composition.assert_single_dispatch()
        return result
    finally:
        composition.close()
