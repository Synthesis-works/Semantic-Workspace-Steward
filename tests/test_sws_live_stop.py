"""Hermetic tests for the live stop composition root and entry point.

No AWS, no network. The mutation client is a local fake exposing exactly the
surface the production objects read: a ``get_paginator("describe_instances")``
seam for the observer, a single ``stop_instances`` for the handler, and a
one-attempt retry configuration the handler verifies. Approval artifacts are
signed with the repository's own Ed25519 helpers (installed via the ``dev``
extra).

Pinned properties:

* ``prepare`` mints a durable PENDING ticket with the exact intent key.
* ``execute`` verifies the artifact before touching the store, grants it, runs
  the real coordinator, and dispatches exactly once to ``VERIFIED_SUCCESS``.
* A tampered artifact, a wrong pinned key, and a snapshot id that does not match
  the signed intent key are refused before any durable state changes.
* An approval signed by an operator identity is refused by the execution gate.
* ``STOP_RESOURCE.implemented`` stays ``False``.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography", reason="the live-stop tests sign approvals")

from sws_agent.approval_signature import (
    ApprovalFields,
    SignedApproval,
    generate_signer_keypair,
    public_key_raw_bytes,
    sign_approval,
)
from sws_agent.audit import LEDGER_FILENAME
from sws_agent.composition import APPROVAL_LEDGER_FILENAME
from sws_agent.constants import PotentialAction
from sws_agent.execution import ACTION_EXECUTION_REGISTRY

from sws_live import cli
from sws_live.live_stop import (
    LiveStopError,
    execute_live_stop,
    parse_instance_arn,
    parse_signed_approval_artifact,
    prepare_live_stop,
)

ARN = "arn:aws:ec2:us-east-1:527557823928:instance/i-0a881305e5bc3c133"
INSTANCE = "i-0a881305e5bc3c133"
ACCOUNT = "527557823928"
REGION = "us-east-1"
EXECUTOR = "i-0c37dfef1dd3fcea7"


def _clock(start: datetime | None = None):
    """A tz-aware clock that advances one second per read."""
    state = {"t": start or datetime(2026, 1, 1, tzinfo=timezone.utc)}

    def tick() -> datetime:
        value = state["t"]
        state["t"] = state["t"] + timedelta(seconds=1)
        return value

    return tick


class _Paginator:
    def __init__(self, client: "_Client") -> None:
        self._client = client

    def paginate(self, **_: object):
        yield {
            "Reservations": [
                {
                    "OwnerId": self._client.account_id,
                    "Instances": [
                        {
                            "InstanceId": self._client.instance_id,
                            "State": {"Name": self._client._next_state()},
                        }
                    ],
                }
            ]
        }


class _Client:
    """Minimal stand-in for the dedicated mutation client."""

    def __init__(self, *, states: list[str] | None = None) -> None:
        self.instance_id = INSTANCE
        self.account_id = ACCOUNT
        self._states = list(states or ["running", "stopping", "stopped"])
        self.stop_calls: list[dict] = []
        self.meta = SimpleNamespace(
            config=SimpleNamespace(retries={"total_max_attempts": 1})
        )

    def _next_state(self) -> str:
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_instances", name
        return _Paginator(self)

    def stop_instances(self, **params):
        self.stop_calls.append(params)
        return {"StoppingInstances": [{"InstanceId": params["InstanceIds"][0]}]}


def _deadline(request: dict) -> datetime:
    return datetime.fromisoformat(request["execution_deadline"])


def _sign(request: dict, private_key, signer_key_id: str, issued_at: datetime) -> SignedApproval:
    fields = ApprovalFields(
        ticket_id=request["ticket_id"],
        resource_id=request["resource_id"],
        action=request["action"],
        plan_id=request["plan_id"],
        execution_intent_key=request["execution_intent_key"],
        execution_deadline=_deadline(request),
        executor_instance_id=request["executor_instance_id"],
        account_id=request["account_id"],
        region=request["region"],
        nonce=request["nonce"],
        issued_at=issued_at,
        signer_key_id=signer_key_id,
    )
    return sign_approval(fields, private_key)


def _case(
    *,
    states: list[str] | None = None,
    snapshot_id: str = "snap-1",
    deadline_minutes: int = 30,
    pin_other_key: bool = False,
    tamper_signature: bool = False,
) -> SimpleNamespace:
    clock = _clock()
    signer_key_id, private_key = generate_signer_keypair()
    if pin_other_key:
        other_id, other_key = generate_signer_keypair()
        pin = {other_id: public_key_raw_bytes(other_key.public_key())}
    else:
        pin = {signer_key_id: public_key_raw_bytes(private_key.public_key())}

    ledger = tempfile.mkdtemp(prefix="sws-live-")
    request = prepare_live_stop(
        ledger_dir=ledger,
        target_instance_arn=ARN,
        region=REGION,
        executor_instance_id=EXECUTOR,
        account_id=ACCOUNT,
        snapshot_id=snapshot_id,
        plan_id="plan-1",
        nonce="nonce-1",
        deadline_minutes=deadline_minutes,
        now=clock,
    )
    issued_at = clock()
    signed = _sign(request, private_key, signer_key_id, issued_at)
    if tamper_signature:
        signed = SignedApproval(
            fields=signed.fields, signature=signed.signature[:-4] + "AAAA"
        )
    return SimpleNamespace(
        ledger=ledger,
        request=request,
        signed=signed,
        pin=pin,
        clock=clock,
        client=_Client(states=states),
        signer_key_id=signer_key_id,
    )


def _execute(case: SimpleNamespace, *, operator: bool = False, snapshot_id: str | None = None):
    return execute_live_stop(
        ledger_dir=case.ledger,
        target_instance_arn=ARN,
        region=REGION,
        executor_instance_id=EXECUTOR,
        account_id=ACCOUNT,
        snapshot_id=snapshot_id or case.request["snapshot_id"],
        signed_approval=case.signed,
        approval_signer_public_keys=case.pin,
        operator_identities=frozenset({case.signer_key_id}) if operator else frozenset(),
        client=case.client,
        arm_witness=False,
        sleep=lambda _seconds: None,
        now=case.clock,
    )


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_mints_a_pending_ticket_with_the_exact_intent_key() -> None:
    case = _case()
    from sws_agent.approval_ledger import DurableApprovalStore
    from sws_agent.execution import execution_intent_key

    expected = execution_intent_key(
        snapshot_id=case.request["snapshot_id"],
        resource_id=INSTANCE,
        action=PotentialAction.STOP_RESOURCE,
    )
    assert case.request["execution_intent_key"] == expected
    assert case.request["action"] == "stop_resource"

    store = DurableApprovalStore(
        case.ledger + "/" + APPROVAL_LEDGER_FILENAME, now=case.clock
    )
    try:
        ticket = store.get(case.request["ticket_id"])
    finally:
        store.close()
    assert ticket.status.value == "pending"
    assert ticket.plan_id == "plan-1"
    assert ticket.execution_intent_key == expected


def test_prepare_refuses_a_region_that_disagrees_with_the_arn() -> None:
    with pytest.raises(LiveStopError):
        prepare_live_stop(
            ledger_dir=tempfile.mkdtemp(),
            target_instance_arn=ARN,
            region="us-west-2",
            executor_instance_id=EXECUTOR,
            account_id=ACCOUNT,
            snapshot_id="snap-1",
        )


def test_parse_instance_arn_rejects_a_non_arn() -> None:
    with pytest.raises(LiveStopError):
        parse_instance_arn("i-0a881305e5bc3c133")


# ---------------------------------------------------------------------------
# execute -- the happy path
# ---------------------------------------------------------------------------


def test_execute_dispatches_once_and_settles_verified_success() -> None:
    case = _case()
    result = _execute(case)

    assert result.outcome.value == "verified_success", result
    assert len(case.client.stop_calls) == 1
    assert case.client.stop_calls[0]["InstanceIds"] == [INSTANCE]

    from sws_agent.approval_ledger import DurableApprovalStore

    store = DurableApprovalStore(
        case.ledger + "/" + APPROVAL_LEDGER_FILENAME, now=case.clock
    )
    try:
        assert store.get(case.request["ticket_id"]).status.value == "consumed"
    finally:
        store.close()


def test_execute_writes_an_audit_ledger() -> None:
    case = _case()
    _execute(case)
    import os

    assert os.path.exists(case.ledger + "/" + LEDGER_FILENAME)


# ---------------------------------------------------------------------------
# execute -- refusals that must happen before the store is touched
# ---------------------------------------------------------------------------


def test_a_tampered_signature_is_refused() -> None:
    case = _case(tamper_signature=True)
    with pytest.raises(LiveStopError):
        _execute(case)
    assert case.client.stop_calls == []


def test_an_artifact_not_signed_by_a_pinned_key_is_refused() -> None:
    case = _case(pin_other_key=True)
    with pytest.raises(LiveStopError):
        _execute(case)
    assert case.client.stop_calls == []


def test_a_snapshot_id_that_does_not_match_the_signed_intent_key_is_refused() -> None:
    case = _case(snapshot_id="snap-original")
    with pytest.raises(LiveStopError):
        _execute(case, snapshot_id="snap-different")
    assert case.client.stop_calls == []


# ---------------------------------------------------------------------------
# execute -- the gate is still the authority
# ---------------------------------------------------------------------------


def test_an_operator_signed_approval_is_refused_by_the_gate() -> None:
    case = _case()
    result = _execute(case, operator=True)
    assert result.outcome.value == "refused"
    assert result.refusal is not None
    assert "approval" in result.refusal.value
    assert case.client.stop_calls == []


def test_program_invariant_stop_resource_is_not_implemented() -> None:
    assert ACTION_EXECUTION_REGISTRY[PotentialAction.STOP_RESOURCE].implemented is False


# ---------------------------------------------------------------------------
# artifact transport
# ---------------------------------------------------------------------------


def test_signed_artifact_round_trips_through_json() -> None:
    import json

    case = _case()
    artifact = {
        "fields": {
            **case.signed.fields.as_payload(),
        },
        "signature": case.signed.signature,
    }
    restored = parse_signed_approval_artifact(json.loads(json.dumps(artifact)))
    assert restored.fields == case.signed.fields
    assert restored.signature == case.signed.signature


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_prepare_writes_a_signing_request(tmp_path) -> None:
    out = tmp_path / "signing_request.json"
    rc = cli.main(
        [
            "prepare",
            "--ledger-dir",
            str(tmp_path / "ledger"),
            "--target-instance-arn",
            ARN,
            "--region",
            REGION,
            "--executor-instance-id",
            EXECUTOR,
            "--account-id",
            ACCOUNT,
            "--snapshot-id",
            "snap-1",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    import json

    request = json.loads(out.read_text(encoding="utf-8"))
    assert request["action"] == "stop_resource"
    assert request["execution_intent_key"]
    assert request["ticket_id"]


def test_cli_pin_parsing_rejects_bad_hex() -> None:
    with pytest.raises(LiveStopError):
        cli._parse_pins(["ed25519:abc=nothex"])
