"""Tests for Candidate 1, Option B: detached approval signatures.

Three layers are covered, in increasing integration depth:

1. **The cryptographic primitives** (``approval_signature``): what gets
   signed, that tampering any bound field is refused, and that only public
   material is needed to *verify*.
2. **The approval stores** (``approval`` / ``approval_ledger``): recording a
   detached artifact durably, refusing replayed nonces and mis-bound or
   over-long artifacts, and the v1 -> v2 schema migration that adds the
   signature columns without touching existing rows.
3. **The execution gate** (``execution`` wired by ``composition``): a signed
   approval crosses the authorization boundary, while unsigned, tampered,
   replayed, wrong-key, and wrong-environment approvals do not.

The last layer is what Option B is *for*: the executor holds only public
keys, and the coordinator only refuses -- it can never mint an approval.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

from sws_agent.approval import ApprovalArtifactError, InMemoryApprovalStore
from sws_agent.audit import JsonlAuditStore
from sws_agent.approval_ledger import APPROVAL_LEDGER_SCHEMA_VERSION
from sws_agent.approval_ledger import DurableApprovalStore
from sws_agent.approval_signature import (
    ApprovalEnvironmentMismatchError,
    ApprovalFields,
    ApprovalSignatureError,
    ApprovalSignatureExpiredError,
    ApprovalSignatureVerificationError,
    ApprovalVerificationPolicy,
    SIGNED_FIELD_NAMES,
    canonical_encode,
    generate_signer_keypair,
    parse_public_key_raw,
    public_key_raw_bytes,
    sign_approval,
    stored_approval_from_ticket,
    verify_approval_for_execution,
)
from sws_agent.composition import (
    CompositionError,
    build_approval_verifier,
    build_dry_run_composition,
    NonMutatingMutationHandler,
)
from sws_agent.constants import (
    ApprovalStatus,
    DispatchDisposition,
    ExecutionMode,
    ExecutionOutcome,
    PotentialAction,
    RefusalReason,
    RiskLevel,
    SWSResourceType,
)
from sws_agent.execution import ExecutionCoordinator
from sws_agent.execution_ledger import DurableExecutionLedger
from sws_agent.models import (
    ActionPlan,
    ApprovalTicket,
    DispatchEvidence,
    ExecutionRequest,
    PolicyDecision,
    ResourceObservation,
    ResourceRecord,
    WorkspaceSnapshot,
)
from sws_agent.workflow import ActionPlanner

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
EC2_ARNS = "arn:aws:ec2:us-east-1:123456789012:instance/i-abc123"
ACCOUNT_ID = "123456789012"
EC2_REGION = "us-east-1"
SNAPSHOT_STARTED = FIXED_NOW - timedelta(minutes=60)
SNAPSHOT_COLLECTED = FIXED_NOW - timedelta(minutes=30)
STOP = PotentialAction.STOP_RESOURCE
EXECUTOR_INSTANCE = "i-exec"


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, value: datetime = FIXED_NOW) -> None:
        self._value = value

    def __call__(self) -> datetime:
        return self._value

    def advance(self, seconds: int) -> None:
        self._value = self._value + timedelta(seconds=seconds)


class _Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _SeqIds:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._counter = count(1)

    def __call__(self) -> str:
        return f"{self._prefix}-{next(self._counter)}"


def _keypair() -> tuple[str, object]:
    signer_id, private_key = generate_signer_keypair()
    return signer_id, private_key


def _key_object(private_key: object) -> object:
    """Recover the public key object for pinning.

    In production the executor pins the *raw* bytes from configuration and
    never sees a private key. Here the public key is recovered from the
    private key that generated the pair, keeping the property "only raw
    bytes cross the wiring boundary" testable.
    """
    return parse_public_key_raw(public_key_raw_bytes(private_key.public_key()))


def _fields(
    ticket: ApprovalTicket,
    *,
    signer_key_id: str,
    deadline: datetime,
    issued_at: datetime,
    nonce: str = "nonce-1",
    **overrides: object,
) -> ApprovalFields:
    base: dict[str, object] = dict(
        ticket_id=ticket.ticket_id,
        resource_id=ticket.resource_id,
        action=ticket.action.value,
        plan_id=ticket.plan_id,
        execution_intent_key=ticket.execution_intent_key,
        execution_deadline=deadline,
        executor_instance_id=EXECUTOR_INSTANCE,
        account_id=ACCOUNT_ID,
        region=EC2_REGION,
        nonce=nonce,
        issued_at=issued_at,
        signer_key_id=signer_key_id,
    )
    base.update(overrides)
    return ApprovalFields(**base)


def _policy(
    *,
    public_keys: dict[str, object],
    executor_instance_id: str = EXECUTOR_INSTANCE,
    account_id: str = ACCOUNT_ID,
    region: str = EC2_REGION,
    operator_identities: frozenset[str] = frozenset(),
    now: Callable[[], datetime] | None = None,
    nonce_lookup: Callable[[str], str | None] | None = None,
) -> ApprovalVerificationPolicy:
    policy = ApprovalVerificationPolicy(
        public_keys={
            key_id: (
                key
                if hasattr(key, "verify")
                else parse_public_key_raw(bytes(key))
            )
            for key_id, key in public_keys.items()
        },
        executor_instance_id=executor_instance_id,
        account_id=account_id,
        region=region,
        operator_identities=operator_identities,
        now=now or (lambda: FIXED_NOW),
    )
    if nonce_lookup is not None:
        object.__setattr__(policy, "nonce_lookup", nonce_lookup)
    return policy


def _approved(
    ticket: ApprovalTicket,
    keypair: tuple[str, object],
    *,
    nonce: str = "nonce-1",
    deadline_offset: timedelta = timedelta(minutes=30),
) -> object:
    signer_id, private_key = keypair
    fields = _fields(
        ticket,
        signer_key_id=signer_id,
        deadline=ticket.execution_deadline or (FIXED_NOW + deadline_offset),
        issued_at=ticket.issued_at or FIXED_NOW,
        nonce=nonce,
    )
    return sign_approval(fields, private_key)


def _make_snapshot() -> WorkspaceSnapshot:
    record = ResourceRecord(
        resource_id="inst-1",
        resource_type=SWSResourceType.EC2_INSTANCE,
        name="instance-one",
        arn=EC2_ARNS,
        account_id=ACCOUNT_ID,
        region=EC2_REGION,
    )
    return WorkspaceSnapshot(
        snapshot_id="snap-1",
        run_id="run-1",
        created_at=SNAPSHOT_STARTED,
        collected_at=SNAPSHOT_COLLECTED,
        regions=[EC2_REGION],
        resource_types=[SWSResourceType.EC2_INSTANCE],
        resources=[record],
        counts={SWSResourceType.EC2_INSTANCE: 1},
        partial=False,
        truncated=False,
    )


def _make_decision(snapshot: WorkspaceSnapshot) -> PolicyDecision:
    return PolicyDecision(
        resource_id="inst-1",
        recommended_action=STOP,
        risk_level=RiskLevel.MEDIUM,
        needs_approval=True,
        decision_id="dec-1",
        snapshot_id=snapshot.snapshot_id,
        run_id=snapshot.run_id,
    )


def _make_plan(
    store: InMemoryApprovalStore,
    *,
    snapshot: WorkspaceSnapshot,
    decision: PolicyDecision,
    plan_id: str = "plan-1",
) -> ActionPlan:
    return ActionPlanner(
        approval_store=store,
        execution_mode=ExecutionMode.SAFE,
        plan_id_source=lambda: plan_id,
        now=lambda: FIXED_NOW,
    ).plan(
        resource_id="inst-1",
        resource_type=SWSResourceType.EC2_INSTANCE,
        action=STOP,
        decision=decision,
    )


def _granted(store: InMemoryApprovalStore, plan: ActionPlan) -> ApprovalTicket:
    return store.grant(plan.ticket.ticket_id, decided_by="human-1")


def _request(
    plan: ActionPlan,
    snapshot: WorkspaceSnapshot,
    decision: PolicyDecision,
    ticket: ApprovalTicket,
) -> ExecutionRequest:
    return ExecutionRequest(
        action_plan_id="plan-1",
        resource_id="inst-1",
        action=STOP,
        execution_mode=plan.execution_mode,
        plan=plan,
        decision=decision,
        snapshot=snapshot,
        ticket=ticket,
        expected_poststate={"state": "stopped"},
    )


class _World:
    """Fake post-mutation world shared by the fake handler and provider."""

    def __init__(self) -> None:
        self.state = "running"
        self.handler_calls: list[ExecutionRequest] = []

    def handle(self, request: ExecutionRequest) -> DispatchEvidence:
        self.handler_calls.append(request)
        self.state = "stopped"
        return DispatchEvidence(
            disposition=DispatchDisposition.ACCEPTED,
            sanitized={"dispatched": True},
        )

    def observe(self, resource_id: str) -> ResourceObservation:
        return ResourceObservation.issued(
            resource_id=resource_id,
            resource_type=SWSResourceType.EC2_INSTANCE,
            facts={"state": self.state},
            observed_at=FIXED_NOW,
            arn=EC2_ARNS,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
        )


def _run(
    store: InMemoryApprovalStore,
    request: ExecutionRequest,
    approval_verifier: Callable[
        [ApprovalTicket], tuple[RefusalReason, str] | None
    ] | None,
    *,
    world: _World | None = None,
) -> tuple[object, _World]:
    """Execute ``request`` through a fresh coordinator and return the result."""
    world = world or _World()
    directory = Path(tempfile.mkdtemp(prefix="sws-gate-"))
    coordinator = ExecutionCoordinator(
        approval_store=store,
        execution_ledger=DurableExecutionLedger(
            directory / "executions.sqlite3",
            now=lambda: FIXED_NOW,
            id_source=_SeqIds("res"),
        ),
        handler=world,
        observer=world,
        audit_store=JsonlAuditStore(
            directory / "audit.jsonl",
            now=lambda: FIXED_NOW,
            id_source=_SeqIds("audit"),
        ),
        id_source=lambda: "exec-1",
        worker_id="worker-1",
        now=_Clock(),
        sleep=_Sleeper(),
        approval_verifier=approval_verifier,
    )
    return coordinator.execute(request), world


# ---------------------------------------------------------------------------
# 1. Cryptographic primitives
# ---------------------------------------------------------------------------


class TestCanonicalEncoding:
    def test_payload_is_exactly_the_signed_field_names(self):
        keypair = _keypair()
        ticket = ApprovalTicket(
            ticket_id="t-1",
            resource_id="i-1",
            action="stop_resource",
            rationale="r",
            created_at=FIXED_NOW,
            plan_id="plan-1",
            execution_intent_key="intent-1",
            evidence_digest=(
                "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            ),
        )
        fields = _fields(
            ticket,
            signer_key_id=keypair[0],
            deadline=FIXED_NOW + timedelta(minutes=30),
            issued_at=FIXED_NOW,
        )
        assert set(fields.as_payload()) == set(SIGNED_FIELD_NAMES)
        # evidence_digest is deliberately NOT part of the approved payload:
        # it is execution evidence, not what the approver authorizes.
        assert "evidence_digest" not in fields.as_payload()

    def test_encoding_is_deterministic_and_domain_separated(self):
        keypair = _keypair()
        ticket = ApprovalTicket(
            ticket_id="t-1",
            resource_id="i-1",
            action="stop_resource",
            rationale="r",
            created_at=FIXED_NOW,
        )
        fields = _fields(
            ticket,
            signer_key_id=keypair[0],
            deadline=FIXED_NOW + timedelta(minutes=30),
            issued_at=FIXED_NOW,
        )
        assert canonical_encode(fields) == canonical_encode(fields)
        assert canonical_encode(fields).startswith(b"sws/approval/v1\n")

    def test_naive_timestamp_is_refused_at_construction(self):
        keypair = _keypair()
        ticket = ApprovalTicket(
            ticket_id="t-1",
            resource_id="i-1",
            action="stop_resource",
            rationale="r",
            created_at=FIXED_NOW,
        )
        with pytest.raises(ApprovalSignatureError):
            ApprovalFields(
                ticket_id=ticket.ticket_id,
                resource_id=ticket.resource_id,
                action=ticket.action.value,
                plan_id=None,
                execution_intent_key=None,
                execution_deadline=(
                    FIXED_NOW.replace(tzinfo=None) + timedelta(minutes=30)
                ),
                executor_instance_id=EXECUTOR_INSTANCE,
                account_id=ACCOUNT_ID,
                region=EC2_REGION,
                nonce="nonce-1",
                issued_at=FIXED_NOW,
                signer_key_id=keypair[0],
            )


class TestSignAndVerify:
    def _ticket(self, *, signer_key_id: str) -> ApprovalTicket:
        return ApprovalTicket(
            ticket_id="t-1",
            resource_id="i-1",
            action="stop_resource",
            rationale="r",
            created_at=FIXED_NOW,
            plan_id="plan-1",
            execution_intent_key="intent-1",
            execution_deadline=FIXED_NOW + timedelta(minutes=10),
            signer_key_id=signer_key_id,
            executor_instance_id=EXECUTOR_INSTANCE,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            nonce="nonce-1",
            issued_at=FIXED_NOW,
        )

    def test_stored_action_unwraps_enum_value(self):
        """str() of a str-mixin enum must not leak into the signed bytes."""
        keypair = _keypair()
        ticket = self._ticket(signer_key_id=keypair[0])
        stored = stored_approval_from_ticket(ticket)
        assert stored.action == "stop_resource"

    def test_round_trip_verifies(self):
        keypair = _keypair()
        ticket = self._ticket(signer_key_id=keypair[0])
        signed = _approved(ticket, keypair, nonce="nonce-1")
        stored = stored_approval_from_ticket(
            ticket.model_copy(update={"signature": signed.signature})
        )
        policy = _policy(
            public_keys={keypair[0]: _key_object(keypair[1])},
            nonce_lookup=lambda n: "t-1",
        )
        verify_approval_for_execution(stored, policy)

    def test_wrong_key_is_refused(self):
        good = _keypair()
        other = _keypair()
        ticket = self._ticket(signer_key_id=good[0])
        signed = _approved(ticket, good, nonce="nonce-1")
        stored = stored_approval_from_ticket(
            ticket.model_copy(update={"signature": signed.signature})
        )
        policy = _policy(public_keys={good[0]: _key_object(other[1])})
        with pytest.raises(ApprovalSignatureVerificationError):
            verify_approval_for_execution(stored, policy)

    def test_tampering_any_signed_field_is_refused(self):
        good = _keypair()
        ticket = self._ticket(signer_key_id=good[0])
        signed = _approved(ticket, good, nonce="nonce-1")

        tamperers: list[tuple[str, dict]] = [
            ("ticket_id", {"ticket_id": "t-2"}),
            ("resource_id", {"resource_id": "i-2"}),
            ("action", {"action": "start_instances"}),
            ("plan_id", {"plan_id": "plan-2"}),
            ("execution_intent_key", {"execution_intent_key": "intent-2"}),
            (
                "execution_deadline",
                {"execution_deadline": FIXED_NOW + timedelta(hours=2)},
            ),
            ("executor_instance_id", {"executor_instance_id": "i-OTHER"}),
            ("account_id", {"account_id": "999999999999"}),
            ("region", {"region": "us-west-2"}),
            ("nonce", {"nonce": "nonce-2"}),
            ("issued_at", {"issued_at": FIXED_NOW + timedelta(days=1)}),
            ("signer_key_id", {"signer_key_id": "ed25519:someone-else"}),
        ]
        for _, update in tamperers:
            tampered_stored = stored_approval_from_ticket(
                ticket.model_copy(update=update | {"signature": signed.signature})
            )
            with pytest.raises(ApprovalSignatureVerificationError):
                verify_approval_for_execution(
                    tampered_stored,
                    _policy(
                        public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
                        nonce_lookup=lambda n: "t-1",
                    ),
                )

    def test_unknown_signer_is_refused(self):
        good = _keypair()
        published = _keypair()
        ticket = self._ticket(signer_key_id=good[0])
        signed = _approved(ticket, good, nonce="nonce-1")
        stored = stored_approval_from_ticket(
            ticket.model_copy(update={"signature": signed.signature})
        )
        policy = _policy(
            public_keys={
                published[0]: public_key_raw_bytes(published[1].public_key())
            },
            nonce_lookup=lambda n: "t-1",
        )
        with pytest.raises(ApprovalSignatureVerificationError):
            verify_approval_for_execution(stored, policy)

    def test_operator_signer_is_refused(self):
        good = _keypair()
        ticket = self._ticket(signer_key_id=good[0])
        signed = _approved(ticket, good, nonce="nonce-1")
        stored = stored_approval_from_ticket(
            ticket.model_copy(update={"signature": signed.signature})
        )
        policy = _policy(
            public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
            operator_identities=frozenset({good[0]}),
            nonce_lookup=lambda n: "t-1",
        )
        with pytest.raises(ApprovalSignatureVerificationError):
            verify_approval_for_execution(stored, policy)

    def test_environment_mismatch_is_refused(self):
        good = _keypair()
        ticket = self._ticket(signer_key_id=good[0])
        signed = _approved(ticket, good, nonce="nonce-1")
        stored = stored_approval_from_ticket(
            ticket.model_copy(update={"signature": signed.signature})
        )
        for kwargs in (
            {"executor_instance_id": "i-OTHER"},
            {"account_id": "999999999999"},
            {"region": "us-west-2"},
        ):
            with pytest.raises(ApprovalEnvironmentMismatchError):
                verify_approval_for_execution(
                    stored,
                    _policy(
                        public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
                        nonce_lookup=lambda n: "t-1",
                        **kwargs,
                    ),
                )

    def test_expired_artifact_is_refused(self):
        good = _keypair()
        ticket = self._ticket(signer_key_id=good[0])
        signed = _approved(ticket, good, nonce="nonce-1")
        stored = stored_approval_from_ticket(
            ticket.model_copy(update={"signature": signed.signature})
        )
        with pytest.raises(ApprovalSignatureExpiredError):
            verify_approval_for_execution(
                stored,
                _policy(
                    public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
                    nonce_lookup=lambda n: "t-1",
                    now=lambda: ticket.execution_deadline
                    + timedelta(seconds=1),
                ),
            )

    def test_nonce_binding_failures_are_refused(self):
        good = _keypair()
        ticket = self._ticket(signer_key_id=good[0])
        signed = _approved(ticket, good, nonce="nonce-1")
        stored = stored_approval_from_ticket(
            ticket.model_copy(update={"signature": signed.signature})
        )
        unrecorded = _policy(
            public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
            nonce_lookup=lambda n: None,
        )
        with pytest.raises(ApprovalSignatureVerificationError):
            verify_approval_for_execution(stored, unrecorded)
        replayed = _policy(
            public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
            nonce_lookup=lambda n: "t-999",
        )
        with pytest.raises(ApprovalSignatureVerificationError):
            verify_approval_for_execution(stored, replayed)

    def test_lazy_import_of_cryptography(self):
        """The signature module imports without ``cryptography`` present.

        The core depends on nothing but pydantic; cryptography is loaded
        lazily inside the signing/verification functions, so a host that never
        verifies an approval does not even load the math.
        """
        src = str(Path(__file__).resolve().parents[0] / "src")
        code = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import sws_agent.approval_signature as m\n"
            "assert 'cryptography' not in sys.modules, 'cryptography eager'\n"
            "print('lazy-ok')\n"
        ) % src
        env = dict(os.environ)
        env["PYTHONPATH"] = ""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert "lazy-ok" in result.stdout

    def test_raw_public_key_length_is_enforced(self):
        with pytest.raises(ApprovalSignatureError):
            parse_public_key_raw(b"short")


# ---------------------------------------------------------------------------
# 2. Approval stores
# ---------------------------------------------------------------------------


class TestStoreIntegration:
    def test_durable_store_records_and_round_trips_a_signed_grant(self, tmp_path: Path):
        store = DurableApprovalStore(
            tmp_path / "approvals.sqlite3",
            now=lambda: FIXED_NOW,
            execution_ttl=timedelta(hours=1),
        )
        ticket = store.create_ticket(
            resource_id="i-1",
            action=STOP,
            rationale="r",
            plan_id="plan-1",
            execution_intent_key="intent-1",
        )
        good = _keypair()
        signed = _approved(ticket, good, nonce="nonce-1")
        granted = store.grant(
            ticket.ticket_id,
            decided_by="alice",
            signed=signed,
        )
        assert granted.status is ApprovalStatus.GRANTED
        assert granted.signature == signed.signature
        assert granted.signer_key_id == good[0]
        assert granted.nonce == "nonce-1"
        assert granted.executor_instance_id == EXECUTOR_INSTANCE
        # The signed window, not the store TTL, is the deadline.
        assert granted.execution_deadline == FIXED_NOW + timedelta(minutes=30)

        reread = store.get(ticket.ticket_id)
        assert store.lookup_nonce("nonce-1") == ticket.ticket_id
        assert store.lookup_nonce("never-seen") is None

        stored = stored_approval_from_ticket(reread)
        policy = _policy(
            public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
            nonce_lookup=store.lookup_nonce,
        )
        verify_approval_for_execution(stored, policy)
        store.close()

    def test_durable_store_refuses_replayed_nonce(self, tmp_path: Path):
        store = DurableApprovalStore(
            tmp_path / "approvals.sqlite3",
            now=lambda: FIXED_NOW,
        )
        one = store.create_ticket(
            resource_id="i-1",
            action=STOP,
            rationale="r",
            plan_id="plan-1",
            execution_intent_key="intent-1",
        )
        good = _keypair()
        store.grant(one.ticket_id, signed=_approved(one, good, nonce="nonce-1"))
        two = store.create_ticket(
            resource_id="i-2",
            action=STOP,
            rationale="r",
            plan_id="plan-2",
            execution_intent_key="intent-2",
        )
        with pytest.raises(ApprovalArtifactError, match="already been recorded"):
            store.grant(two.ticket_id, signed=_approved(two, good, nonce="nonce-1"))

    def test_durable_store_refuses_misbound_artifact(self, tmp_path: Path):
        store = DurableApprovalStore(
            tmp_path / "approvals.sqlite3",
            now=lambda: FIXED_NOW,
        )
        ticket = store.create_ticket(
            resource_id="i-1",
            action=STOP,
            rationale="r",
            plan_id="plan-1",
            execution_intent_key="intent-1",
        )
        good = _keypair()
        signer_id, private_key = good
        wrong = _fields(
            ticket,
            signer_key_id=signer_id,
            deadline=FIXED_NOW + timedelta(minutes=30),
            issued_at=FIXED_NOW,
            nonce="nonce-1",
            resource_id="i-NOT-IT",
        )
        with pytest.raises(ApprovalArtifactError, match="does not bind"):
            store.grant(ticket.ticket_id, signed=sign_approval(wrong, private_key))

    def test_durable_store_refuses_overlong_window(self, tmp_path: Path):
        store = DurableApprovalStore(
            tmp_path / "approvals.sqlite3",
            now=lambda: FIXED_NOW,
            execution_ttl=timedelta(hours=1),
        )
        ticket = store.create_ticket(
            resource_id="i-1",
            action=STOP,
            rationale="r",
            plan_id="plan-1",
            execution_intent_key="intent-1",
        )
        good = _keypair()
        signer_id, private_key = good
        wide = _fields(
            ticket,
            signer_key_id=signer_id,
            deadline=FIXED_NOW + timedelta(hours=5),
            issued_at=FIXED_NOW,
            nonce="nonce-1",
        )
        with pytest.raises(ApprovalArtifactError, match="ceiling"):
            store.grant(ticket.ticket_id, signed=sign_approval(wide, private_key))

    def test_durable_store_unsigned_grant_still_works(self, tmp_path: Path):
        store = DurableApprovalStore(
            tmp_path / "approvals.sqlite3",
            now=lambda: FIXED_NOW,
        )
        ticket = store.create_ticket(
            resource_id="i-1",
            action=STOP,
            rationale="r",
            plan_id="plan-1",
            execution_intent_key="intent-1",
        )
        granted = store.grant(ticket.ticket_id, decided_by="bob")
        assert granted.signature is None
        assert granted.signer_key_id is None
        assert granted.nonce is None
        store.close()

    def test_in_memory_store_parity_with_durable(self):
        store = InMemoryApprovalStore(
            now=lambda: FIXED_NOW, execution_ttl=timedelta(hours=1)
        )
        ticket = store.create_ticket(
            resource_id="i-1",
            action=STOP,
            rationale="r",
            plan_id="plan-1",
            execution_intent_key="intent-1",
        )
        good = _keypair()
        signed = _approved(ticket, good, nonce="nonce-1")
        granted = store.grant(ticket.ticket_id, decided_by="alice", signed=signed)
        assert granted.signature == signed.signature
        assert granted.signer_key_id == good[0]
        assert granted.execution_deadline == FIXED_NOW + timedelta(minutes=30)

    def test_v1_to_v2_migration_preserves_ledger(self, tmp_path: Path):
        import sqlite3

        path = tmp_path / "legacy.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE ticket (
                    ticket_id TEXT PRIMARY KEY,
                    resource_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT NOT NULL,
                    decision_reason TEXT NOT NULL,
                    plan_id TEXT,
                    consumed INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    execution_intent_key TEXT,
                    evidence_digest TEXT,
                    execution_deadline TEXT
                );
                CREATE TABLE ticket_event (
                    event_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL REFERENCES ticket(ticket_id),
                    revision INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    plan_id TEXT,
                    decided_by TEXT NOT NULL,
                    decision_reason TEXT NOT NULL,
                    execution_intent_key TEXT,
                    evidence_digest TEXT,
                    execution_deadline TEXT,
                    UNIQUE (ticket_id, revision)
                );
                CREATE INDEX ticket_event_by_ticket
                    ON ticket_event (ticket_id, revision);
                INSERT INTO meta (key, value) VALUES ('schema_version', '1');
                """
            )
            conn.execute(
                "INSERT INTO ticket VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-1",
                    "i-legacy",
                    "stop_resource",
                    "pre-migration",
                    "granted",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:05:00+00:00",
                    "carol",
                    "approved",
                    "plan-legacy",
                    0,
                    1,
                    "intent-legacy",
                    "digest-legacy",
                    "2026-01-01T01:00:00+00:00",
                ),
            )
            for event in (
                (
                    "ev-0",
                    "legacy-1",
                    0,
                    "issued",
                    "2026-01-01T00:00:00+00:00",
                    "pending",
                    "i-legacy",
                    "stop_resource",
                    "plan-legacy",
                    "",
                    "",
                    "intent-legacy",
                    "digest-legacy",
                    None,
                ),
                (
                    "ev-1",
                    "legacy-1",
                    1,
                    "granted",
                    "2026-01-01T00:05:00+00:00",
                    "granted",
                    "i-legacy",
                    "stop_resource",
                    "plan-legacy",
                    "carol",
                    "approved",
                    "intent-legacy",
                    "digest-legacy",
                    "2026-01-01T01:00:00+00:00",
                ),
            ):
                conn.execute(
                    "INSERT INTO ticket_event VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    event,
                )

        store = DurableApprovalStore(path, now=lambda: FIXED_NOW)
        assert APPROVAL_LEDGER_SCHEMA_VERSION == 2
        with sqlite3.connect(path) as conn:
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            assert version == "2"
            has_nonce = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='approval_nonce'"
            ).fetchone()
            assert has_nonce is not None
        legacy = store.get("legacy-1")
        assert legacy.resource_id == "i-legacy"
        # NULL signature fields are the honest representation of a
        # pre-signing approval, not a rewrite of old data.
        assert legacy.signature is None
        assert legacy.nonce is None
        store.verify()
        store.close()


# ---------------------------------------------------------------------------
# 3. Execution gate wiring
# ---------------------------------------------------------------------------


class TestCompositionVerifier:
    def test_verifier_requires_identity_when_keys_are_pinned(self):
        good = _keypair()
        raw = {good[0]: public_key_raw_bytes(good[1].public_key())}
        with pytest.raises(ApprovalSignatureVerificationError, match="executor_instance_id"):
            build_approval_verifier(
                public_keys=raw,
                executor_instance_id="",
                account_id=ACCOUNT_ID,
                region=EC2_REGION,
            )
        with pytest.raises(ApprovalSignatureVerificationError, match="account_id"):
            build_approval_verifier(
                public_keys=raw,
                executor_instance_id=EXECUTOR_INSTANCE,
                account_id="",
                region=EC2_REGION,
            )

    def test_verifier_maps_distinct_failures_to_distinct_reasons(self):
        good = _keypair()
        raw = {good[0]: public_key_raw_bytes(good[1].public_key())}
        verifier = build_approval_verifier(
            public_keys=raw,
            executor_instance_id=EXECUTOR_INSTANCE,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            now=lambda: FIXED_NOW,
            nonce_lookup=lambda n: "t-1" if n == "nonce-1" else None,
        )
        unsigned = ApprovalTicket(
            ticket_id="t-1",
            resource_id="i-1",
            action="stop_resource",
            rationale="r",
            created_at=FIXED_NOW,
            plan_id="plan-1",
            execution_intent_key="intent-1",
        )
        signed_ticket = unsigned.model_copy(
            update={
                "execution_deadline": FIXED_NOW + timedelta(minutes=10),
                "signer_key_id": good[0],
                "executor_instance_id": EXECUTOR_INSTANCE,
                "account_id": ACCOUNT_ID,
                "region": EC2_REGION,
                "nonce": "nonce-1",
                "issued_at": FIXED_NOW,
            }
        )
        artifact = _approved(signed_ticket, good, nonce="nonce-1")
        signed_ticket = signed_ticket.model_copy(
            update={"signature": artifact.signature}
        )

        refusal = verifier(unsigned)
        assert refusal is not None
        assert refusal[0] is RefusalReason.APPROVAL_SIGNATURE_MISSING

        good_refusal = verifier(signed_ticket)
        assert good_refusal is None

        unknown_signer = signed_ticket.model_copy(
            update={"signer_key_id": "ed25519:unpinned"}
        )
        refusal = verifier(unknown_signer)
        assert refusal is not None and refusal[0] is RefusalReason.APPROVAL_SIGNATURE_INVALID

        operator = build_approval_verifier(
            public_keys=raw,
            executor_instance_id=EXECUTOR_INSTANCE,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            operator_identities=frozenset({good[0]}),
            now=lambda: FIXED_NOW,
            nonce_lookup=lambda n: "t-1",
        )
        refusal = operator(signed_ticket)
        assert refusal is not None and refusal[0] is RefusalReason.APPROVAL_SIGNER_FORBIDDEN

        wrong_env = build_approval_verifier(
            public_keys=raw,
            executor_instance_id="i-OTHER",
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            now=lambda: FIXED_NOW,
            nonce_lookup=lambda n: "t-1",
        )
        refusal = wrong_env(signed_ticket)
        assert refusal is not None and refusal[0] is RefusalReason.APPROVAL_ENVIRONMENT_MISMATCH

        # A signature valid over its *unchanged* fields whose window has
        # already closed is EXPIRED (the tamper path is INVALID instead).
        expired = build_approval_verifier(
            public_keys=raw,
            executor_instance_id=EXECUTOR_INSTANCE,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            now=lambda: FIXED_NOW + timedelta(hours=2),
            nonce_lookup=lambda n: "t-1",
        )
        refusal = expired(signed_ticket)
        assert refusal is not None and refusal[0] is RefusalReason.APPROVAL_SIGNATURE_EXPIRED

    def test_verifier_rejects_unknown_nonce(self):
        good = _keypair()
        raw = {good[0]: public_key_raw_bytes(good[1].public_key())}
        verifier = build_approval_verifier(
            public_keys=raw,
            executor_instance_id=EXECUTOR_INSTANCE,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            now=lambda: FIXED_NOW,
            nonce_lookup=lambda n: None,
        )
        ticket = ApprovalTicket(
            ticket_id="t-1",
            resource_id="i-1",
            action="stop_resource",
            rationale="r",
            created_at=FIXED_NOW,
            plan_id="plan-1",
            execution_intent_key="intent-1",
            execution_deadline=FIXED_NOW + timedelta(minutes=10),
            signer_key_id=good[0],
            executor_instance_id=EXECUTOR_INSTANCE,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            nonce="nonce-1",
            issued_at=FIXED_NOW,
        )
        artifact = _approved(ticket, good, nonce="nonce-1")
        refusal = verifier(
            ticket.model_copy(update={"signature": artifact.signature})
        )
        assert refusal is not None
        assert refusal[0] is RefusalReason.APPROVAL_SIGNATURE_INVALID


class TestExecutionGate:
    def test_signed_approval_crosses_the_boundary(self):
        store = InMemoryApprovalStore(
            now=lambda: FIXED_NOW, execution_ttl=timedelta(hours=1)
        )
        snapshot = _make_snapshot()
        decision = _make_decision(snapshot)
        plan = _make_plan(store, snapshot=snapshot, decision=decision)
        good = _keypair()
        granted = store.grant(
            plan.ticket.ticket_id,
            decided_by="human-1",
            signed=_approved(plan.ticket, good, nonce="nonce-gate"),
        )
        verifier = build_approval_verifier(
            public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
            executor_instance_id=EXECUTOR_INSTANCE,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            now=lambda: FIXED_NOW,
            nonce_lookup=lambda n: plan.ticket.ticket_id if n == "nonce-gate" else None,
        )
        result, world = _run(
            store, _request(plan, snapshot, decision, granted), verifier
        )
        assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
        assert len(world.handler_calls) == 1

    def test_unsigned_rejected_at_gate_when_pinned(self):
        store = InMemoryApprovalStore(now=lambda: FIXED_NOW)
        snapshot = _make_snapshot()
        decision = _make_decision(snapshot)
        plan = _make_plan(store, snapshot=snapshot, decision=decision)
        granted = _granted(store, plan)
        good = _keypair()
        verifier = build_approval_verifier(
            public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
            executor_instance_id=EXECUTOR_INSTANCE,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            now=lambda: FIXED_NOW,
            nonce_lookup=lambda n: None,
        )
        result, world = _run(
            store, _request(plan, snapshot, decision, granted), verifier
        )
        assert result.outcome is ExecutionOutcome.REFUSED
        assert result.refusal is RefusalReason.APPROVAL_SIGNATURE_MISSING
        assert world.handler_calls == []

    def test_wrong_environment_rejected_at_gate(self):
        store = InMemoryApprovalStore(now=lambda: FIXED_NOW)
        snapshot = _make_snapshot()
        decision = _make_decision(snapshot)
        plan = _make_plan(store, snapshot=snapshot, decision=decision)
        good = _keypair()
        granted = store.grant(
            plan.ticket.ticket_id,
            decided_by="human-1",
            signed=_approved(plan.ticket, good, nonce="nonce-gate"),
        )
        verifier = build_approval_verifier(
            public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
            executor_instance_id="i-OTHER",
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            now=lambda: FIXED_NOW,
            nonce_lookup=lambda n: plan.ticket.ticket_id if n == "nonce-gate" else None,
        )
        result, world = _run(
            store, _request(plan, snapshot, decision, granted), verifier
        )
        assert result.outcome is ExecutionOutcome.REFUSED
        assert result.refusal is RefusalReason.APPROVAL_ENVIRONMENT_MISMATCH
        assert world.handler_calls == []

    def test_replayed_nonce_rejected_at_gate(self):
        store = InMemoryApprovalStore(now=lambda: FIXED_NOW)
        snapshot = _make_snapshot()
        decision = _make_decision(snapshot)
        plan = _make_plan(store, snapshot=snapshot, decision=decision)
        good = _keypair()
        granted = store.grant(
            plan.ticket.ticket_id,
            decided_by="human-1",
            signed=_approved(plan.ticket, good, nonce="nonce-gate"),
        )
        verifier = build_approval_verifier(
            public_keys={good[0]: public_key_raw_bytes(good[1].public_key())},
            executor_instance_id=EXECUTOR_INSTANCE,
            account_id=ACCOUNT_ID,
            region=EC2_REGION,
            now=lambda: FIXED_NOW,
            nonce_lookup=lambda n: "some-other-ticket",
        )
        result, world = _run(
            store, _request(plan, snapshot, decision, granted), verifier
        )
        assert result.outcome is ExecutionOutcome.REFUSED
        assert result.refusal is RefusalReason.APPROVAL_SIGNATURE_INVALID
        assert world.handler_calls == []

    def test_without_keys_gate_behaves_as_before(self):
        """No pinned keys -> no approval_verifier -> the M12 ticket gate rules."""
        store = InMemoryApprovalStore(now=lambda: FIXED_NOW)
        snapshot = _make_snapshot()
        decision = _make_decision(snapshot)
        plan = _make_plan(store, snapshot=snapshot, decision=decision)
        granted = _granted(store, plan)
        result, world = _run(
            store, _request(plan, snapshot, decision, granted), None
        )
        assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
        assert len(world.handler_calls) == 1


class _ReadOnlyClient:
    """EC2 seam exposing exactly the single permitted read method."""

    def describe_instances(self) -> None:  # pragma: no cover - never called
        return None


class _RefusingHandler(NonMutatingMutationHandler):
    def handle(self, request: ExecutionRequest) -> DispatchEvidence:  # pragma: no cover
        raise AssertionError("handler must not be reached")


class TestDryRunCompositionWiring:
    def test_composition_arms_the_gate_with_pinned_keys(self, tmp_path: Path):
        good = _keypair()
        raw = {good[0]: public_key_raw_bytes(good[1].public_key())}
        composition = build_dry_run_composition(
            ledger_dir=tmp_path / "ledgers",
            ec2_client=_ReadOnlyClient(),
            region=EC2_REGION,
            approval_signer_public_keys=raw,
            executor_instance_id=EXECUTOR_INSTANCE,
            account_id=ACCOUNT_ID,
            handler=_RefusingHandler(),
        )
        coordinator = composition.coordinator
        assert coordinator._approval_verifier is not None
        ticket = composition.approval_store.create_ticket(
            resource_id="inst-1",
            action=STOP,
            rationale="r",
            plan_id="plan-1",
            execution_intent_key="intent-1",
        )
        refusal = coordinator._approval_verifier(ticket)
        assert refusal == (RefusalReason.APPROVAL_SIGNATURE_MISSING, refusal[1])
        assert refusal[0] is RefusalReason.APPROVAL_SIGNATURE_MISSING

    def test_composition_without_keys_has_no_verifier(self, tmp_path: Path):
        composition = build_dry_run_composition(
            ledger_dir=tmp_path / "ledgers",
            ec2_client=_ReadOnlyClient(),
            region=EC2_REGION,
            handler=_RefusingHandler(),
        )
        assert composition.coordinator._approval_verifier is None

    def test_composition_refuses_keys_without_identity(self, tmp_path: Path):
        good = _keypair()
        raw = {good[0]: public_key_raw_bytes(good[1].public_key())}
        with pytest.raises(CompositionError, match="executor_instance_id"):
            build_dry_run_composition(
                ledger_dir=tmp_path / "ledgers",
                ec2_client=_ReadOnlyClient(),
                region=EC2_REGION,
                approval_signer_public_keys=raw,
                account_id=ACCOUNT_ID,
                handler=_RefusingHandler(),
            )