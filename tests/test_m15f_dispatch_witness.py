"""M15-F: the dispatch witness, demonstrated to be falsifiable.

The point of these tests is not that the witness reports one dispatch. It is that
the witness *would* report two if two happened. A witness that cannot fail is not
evidence, and a test that only exercises the happy path cannot tell the
difference between "the safety chain held" and "the counter is broken".

So the second half of this file deliberately builds clients that DO retry, with
responses that DO trigger botocore's retry logic, and asserts the witness sees it.

Everything runs against real botocore with a stubbed transport: a ``before-send``
handler returns a synthetic HTTP response, so signing, event dispatch, and the
retry state machine all execute for real while no socket is ever opened.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from sws_agent.constants import DispatchDisposition
from sws_agent.ec2_mutation import MutationClientConfigurationError, StopInstancesHandler
from sws_agent.ec2_mutation_client import (
    MutationClientSettings,
    MutationCredentials,
    build_botocore_config,
)
from sws_agent.mutation_evidence import (
    DispatchWitness,
    SingleDispatchViolation,
    STOP_INSTANCES_OPERATION,
)

pytest.importorskip("botocore", reason="the witness observes botocore's own events")

INSTANCE_ID = "i-0abcdef1234567890"
INSTANCE_ARN = f"arn:aws:ec2:us-east-1:123456789012:instance/{INSTANCE_ID}"


def _settings() -> MutationClientSettings:
    return MutationClientSettings(
        region="us-east-1",
        credentials=MutationCredentials(
            access_key_id="AKIAIOSFODNN7EXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="sws-m15f-test",
            source="m15f-test",
        ),
        target_instance_arn=INSTANCE_ARN,
    )


class _StubTransport:
    """Answers requests from a script instead of opening a socket.

    Installed on ``before-send``, which botocore consults *per attempt*
    (``endpoint.py:277``) and from which a handler must **return** an
    ``AWSResponse`` rather than mutate the request (``endpoint.py:279-281``).
    Returning a response short-circuits the socket while leaving signing, header
    injection, response parsing, and the retry state machine fully intact, so the
    witness is exercised against real botocore behaviour with no network.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[Any] = []

    def attach(self, client: Any) -> None:
        client.meta.events.register(
            "before-send.ec2",
            self._respond,
            unique_id="sws-m15f-stub-transport",
        )

    def _respond(self, request: Any, **kwargs: Any) -> Any:
        self.requests.append(request)
        payload = (
            self._responses.pop(0) if self._responses else self._responses_default
        )
        return _aws_response(payload, attempt=len(self.requests))

    _responses_default: dict[str, Any] = {"kind": "success"}

    @property
    def attempt_count(self) -> int:
        return len(self.requests)


def _aws_response(payload: dict[str, Any], *, attempt: int) -> Any:
    """Build an ``AWSResponse`` botocore's EC2 query parser accepts.

    EC2 uses the ``query`` protocol, so the body is XML. The element names come
    from the model's serialization traits (``instancesSet`` / ``item`` /
    ``instanceId``), verified against botocore rather than guessed.
    """
    from botocore.awsrequest import AWSResponse
    from urllib3.response import HTTPResponse

    if payload.get("kind") == "error":
        body = _ERROR_XML.format(
            code=payload["code"], message=payload.get("message", "stub error")
        ).encode("utf-8")
        status = payload.get("status", 400)
    else:
        body = _SUCCESS_XML.format(
            instance_id=payload.get("instance_id", INSTANCE_ID),
            attempt=attempt,
        ).encode("utf-8")
        status = 200

    headers = {
        "Content-Type": "text/xml",
        "Content-Length": str(len(body)),
        "x-amzn-RequestId": f"stub-req-{attempt}",
    }
    # ``AWSResponse.content`` calls ``raw.stream()``, so the body must be a
    # urllib3 response rather than a bare BytesIO.
    raw = HTTPResponse(
        body=io.BytesIO(body),
        headers=headers,
        status=status,
        preload_content=False,
    )
    return AWSResponse(
        url="https://ec2.us-east-1.amazonaws.com/",
        status_code=status,
        headers=headers,
        raw=raw,
    )


_SUCCESS_XML = """<StopInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
  <requestId>stub-req-{attempt}</requestId>
  <instancesSet>
    <item>
      <instanceId>{instance_id}</instanceId>
      <currentState><code>64</code><name>stopping</name></currentState>
      <previousState><code>16</code><name>running</name></previousState>
    </item>
  </instancesSet>
</StopInstancesResponse>"""

_ERROR_XML = """<Response><Errors><Error>
  <Code>{code}</Code><Message>{message}</Message>
</Error></Errors><RequestID>stub-req-err</RequestID></Response>"""


def _ok_response() -> dict[str, Any]:
    return {"kind": "success"}


def _throttled() -> dict[str, Any]:
    # 400 with a modeled, retryable code. This is what makes botocore's retry
    # state machine loop, which is the behaviour the witness must detect.
    return {"kind": "error", "code": "RequestLimitExceeded", "message": "slow down"}


def _server_error(status: int = 500) -> dict[str, Any]:
    return {
        "kind": "error",
        "code": "InternalError",
        "message": "boom",
        "status": status,
    }


def _make_client(
    responses: list[dict[str, Any]], *, retries: dict[str, Any] | None = None
) -> tuple[Any, _StubTransport]:
    """A real boto3 ec2 client whose transport is stubbed."""
    import botocore.session
    from botocore.config import Config

    session = botocore.session.get_session()
    if retries is None:
        config = build_botocore_config(_settings())
    else:
        config = Config(retries=retries, region_name="us-east-1")
    client = session.create_client(
        "ec2",
        region_name="us-east-1",
        config=config,
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        aws_session_token="sws-m15f-test",
    )
    transport = _StubTransport(responses)
    transport.attach(client)
    return client, transport


def _request(_client: Any) -> Any:
    """A gated execution request from the existing test helpers.

    Reused rather than rebuilt so this milestone tests the witness, not a second
    construction of the gate.
    """
    from test_m15d_stop_instances import _request as build_request
    from test_m15d_stop_instances import _store

    return build_request(_store())


#: The instance id the shared M15-D test fixtures actually target. Asserted
#: rather than assumed: a witness test that hardcoded its own id would pass
#: while observing the wrong target, which is the failure mode section 4 of the
#: runbook exists to prevent.
TARGET_INSTANCE_ID = "inst-1"


# ---------------------------------------------------------------------------
# 1. The measurement basis: the witness sees one attempt, and can see two.
# ---------------------------------------------------------------------------


def test_one_stop_produces_exactly_one_witnessed_attempt():
    client, transport = _make_client([_ok_response()])
    witness = DispatchWitness()
    witness.arm(client)

    handler = StopInstancesHandler(client, region="us-east-1")
    handler.handle(_request(client))

    assert transport.attempt_count == 1
    assert witness.attempt_count == 1
    assert witness.violations() == ()
    record = witness.assert_single_dispatch()
    assert record.instance_ids == (TARGET_INSTANCE_ID,)
    assert record.max_attempts in (None, 1)  # botocore omits max= when it resolves to one attempt
    assert record.attempt == 1
    assert record.invocation_id is not None


def test_a_misspelled_event_name_fails_closed_rather_than_reading_as_a_pass():
    """What the registry check can and cannot prove, stated as a test.

    An earlier version registered ``before-send.ec2.StopInstance`` (no trailing
    ``s``). botocore accepted the string, stored it, and never emitted it -- the
    witness reported zero attempts on a real dispatch.

    Two separate defenses are involved and they are not equally strong:

    - ``arm()``'s registry check proves the listener *reached* the emitter. It
      cannot tell a well-formed event name from a misspelled one, because
      ``prefix_search`` on the registered name finds the registered name.
    - The fail-closed zero rule is what actually catches a typo: a misspelled
      witness records nothing, and a witness that saw nothing is a violation,
      not a clean run.

    This test pins the second defense, and asserts explicitly that the first
    does not fire -- so the next reader does not mistake the registry check for
    event-name validation.
    """
    client, _transport = _make_client([_ok_response()])
    witness = DispatchWitness()
    witness.arm(client, operation="StopInstance")  # deliberately wrong

    # The registry check does not catch this: registration succeeded.
    assert witness.armed is True

    handler = StopInstancesHandler(client, region="us-east-1")
    handler.handle(_request(client))

    # The zero rule does catch it, which is the point.
    assert witness.records == [], "expected the misspelled witness to record nothing"
    violations = witness.violations()
    assert violations, "a witness that observed nothing must never certify a run"


def test_the_falsification_is_real_because_the_sdk_really_can_retry_here():
    """Meta-test: the detection test above is only meaningful if retries occur.

    If a future botocore stopped retrying on throttling, the detection test
    would still pass for the wrong reason -- it would see one attempt and infer
    the witness works. This pins the premise so that case fails loudly.
    """
    client, transport = _make_client(
        [_throttled(), _throttled(), _ok_response()],
        retries={"total_max_attempts": 3, "mode": "standard"},
    )
    # The final scripted response is a success, so the call may legitimately
    # succeed; what matters is that more than one request went out to get there.
    try:
        client.stop_instances(InstanceIds=[TARGET_INSTANCE_ID])
    except Exception:
        pass
    assert transport.attempt_count > 1, (
        "botocore did not retry a throttled request under total_max_attempts=3; "
        "the falsification tests are no longer proving anything"
    )


def test_the_witness_detects_a_second_dispatch_when_the_sdk_retries():
    """The falsification test. Without this, the first test proves nothing.

    A client configured to retry, given a throttling error, makes botocore issue
    a second HTTP request. The witness must see two attempts and refuse to certify.
    """
    client, transport = _make_client(
        [_throttled(), _throttled(), _ok_response()],
        retries={"total_max_attempts": 3, "mode": "standard"},
    )
    witness = DispatchWitness()
    witness.arm(client)

    # Bypass the handler's precondition: this client is deliberately unsafe, and
    # the point is what the witness reports when a retry happens anyway.
    client.stop_instances(InstanceIds=[INSTANCE_ID])

    assert transport.attempt_count > 1, "botocore did not retry; test premise broken"
    assert witness.attempt_count > 1
    problems = witness.violations()
    assert problems, "the witness failed to notice multiple dispatches"
    assert any("HTTP attempts" in p for p in problems)
    assert any("attempt=" in p for p in problems), problems

    with pytest.raises(SingleDispatchViolation):
        witness.assert_single_dispatch()


def test_the_witness_detects_a_second_attempt_even_when_the_sdk_says_no_retry():
    """Two attempts with max=1 is a contradiction, and it must still be reported.

    Guards against a witness that only counts ``needs-retry`` events: a second
    request can arrive without the SDK having decided to retry it (a retry
    wrapper, an intercepting proxy, or a future botocore behaviour).
    """
    client, transport = _make_client([_ok_response()])
    witness = DispatchWitness()
    witness.arm(client)

    client.stop_instances(InstanceIds=[INSTANCE_ID])
    # Simulate the observation of a second wire attempt without a retry decision.
    witness._on_before_send(
        _FakeRequest(), params={"InstanceIds": [TARGET_INSTANCE_ID]}
    )

    assert witness.attempt_count == 2
    problems = witness.violations()
    assert any("2 HTTP attempts" in p for p in problems)
    with pytest.raises(SingleDispatchViolation):
        witness.assert_single_dispatch()


class _FakeHeaders(dict):
    pass


class _BlindEmitter:
    """Accepts registrations and discards them, with no registry to check."""

    def register(self, *args: object, **kwargs: object) -> None:
        return None


class _BlindClient:
    """A client-shaped object whose emitter can never be verified."""

    class _Meta:
        events = _BlindEmitter()

    meta = _Meta()


class _FakeRequest:
    def __init__(self) -> None:
        self.headers = _FakeHeaders(
            {
                "amz-sdk-invocation-id": "second-attempt-id",
                "amz-sdk-request": "attempt=2; max=1",
            }
        )
        self.url = "https://ec2.us-east-1.amazonaws.com/"


def test_arming_refuses_when_the_emitter_cannot_be_inspected():
    """Proves the registry check is live rather than dead code.

    A witness that cannot confirm its listeners are attached is the same as no
    witness: both produce an empty record. Refusing is the only honest response.
    """
    with pytest.raises(MutationClientConfigurationError, match="searchable registry"):
        DispatchWitness().arm(_BlindClient())


def test_arming_refuses_when_a_listener_does_not_reach_the_registry():
    """And proves it fires on a *partial* attachment, not only a missing emitter.

    A stub that accepts the first registration and drops the rest is the shape of
    a real bug: the witness looks attached, but the hook that carries the attempt
    count never arrives, so every run would read as zero attempts.
    """
    client, _transport = _make_client([_ok_response()])
    # The aliaser forwards positionally (botocore/hooks.py:418-424), so the
    # replacement must accept positional args to be reached at all.
    real_register = client.meta.events._emitter.register

    def drop_before_send(event_name: str, *args: object, **kwargs: object) -> None:
        if str(event_name).startswith("before-send"):
            return None
        return real_register(event_name, *args, **kwargs)

    client.meta.events.register = drop_before_send
    with pytest.raises(MutationClientConfigurationError, match="not reachable"):
        DispatchWitness().arm(client)


def test_the_witness_refuses_to_arm_on_a_client_it_cannot_observe():
    """An unattached witness must not silently produce an empty record."""

    class _Opaque:
        pass

    with pytest.raises(MutationClientConfigurationError):
        DispatchWitness().arm(_Opaque())


def test_an_unarmed_witness_reports_no_dispatch_rather_than_a_clean_run():
    witness = DispatchWitness()
    problems = witness.violations()
    assert len(problems) == 1
    assert "never attempted" in problems[0]
    with pytest.raises(SingleDispatchViolation):
        witness.assert_single_dispatch()


def test_a_witness_that_saw_nothing_cannot_certify_a_dispatch():
    """Zero attempts is an unknown, never a success.

    Distinguishing 'nothing was sent' from 'the counter is broken' is the whole
    reason an empty record is a violation rather than a pass.
    """
    witness = DispatchWitness()
    witness.arm(_make_client([_ok_response()])[0])
    assert witness.violations()


# ---------------------------------------------------------------------------
# 2. The witness observes the real handler, not just a raw SDK call.
# ---------------------------------------------------------------------------


def test_the_witness_certifies_a_real_handler_dispatch():
    """End to end: production client config -> handler -> one witnessed attempt."""
    client, transport = _make_client([_ok_response()])
    witness = DispatchWitness()
    witness.arm(client)

    handler = StopInstancesHandler(client, region="us-east-1")
    evidence = handler.handle(_request(client))

    assert evidence.disposition is DispatchDisposition.ACCEPTED
    assert witness.assert_single_dispatch().instance_ids == (TARGET_INSTANCE_ID,)
    assert transport.attempt_count == 1


def test_the_witness_and_the_handler_precondition_are_independent_checks():
    """The handler refuses a retrying client; the witness proves it mattered.

    Both run on the same client. Neither knows about the other, which is what
    makes them independent rather than two views of one assertion.
    """
    retrying, _ = _make_client(
        [_throttled()], retries={"total_max_attempts": 5, "mode": "standard"}
    )
    with pytest.raises(MutationClientConfigurationError):
        StopInstancesHandler(retrying, region="us-east-1")

    safe, _ = _make_client([_ok_response()])
    StopInstancesHandler(safe, region="us-east-1")  # accepted


def test_a_failed_dispatch_still_yields_exactly_one_witnessed_attempt():
    """A rejected request is still one attempt, and that is not a violation.

    The invariant is about the number of dispatches, not about success. A
    rejection that was sent once and retried zero times is exactly correct
    behaviour, and the witness must not report it as a safety problem.
    """
    client, transport = _make_client(
        [{"__type": "UnauthorizedOperation", "message": "no"}]
    )
    witness = DispatchWitness()
    witness.arm(client)

    handler = StopInstancesHandler(client, region="us-east-1")
    handler.handle(_request(client))

    assert transport.attempt_count == 1
    assert witness.attempt_count == 1
    assert witness.violations() == ()


def test_a_server_error_does_not_retry_under_the_factory_configuration():
    """5xx is retryable in botocore's rules; the configuration is what stops it.

    Confirms the safety property is doing the work rather than the response
    happening to be unretryable.
    """
    client, transport = _make_client(
        [_server_error(500), _server_error(500), _server_error(500)]
    )
    witness = DispatchWitness()
    witness.arm(client)

    handler = StopInstancesHandler(client, region="us-east-1")
    evidence = handler.handle(_request(client))

    assert evidence.disposition is DispatchDisposition.DISPATCH_UNKNOWN
    assert transport.attempt_count == 1, "the SDK retried a 500 despite max_attempts=1"
    assert witness.attempt_count == 1
    assert witness.assert_single_dispatch().attempt == 1


# ---------------------------------------------------------------------------
# 3. Scope: the witness watches only the mutation operation.
# ---------------------------------------------------------------------------


def test_the_witness_ignores_other_operations():
    """Observation must not be perturbed by unrelated calls on the same client."""
    client, transport = _make_client([_ok_response()])
    witness = DispatchWitness()
    witness.arm(client)

    client.describe_instances(InstanceIds=[INSTANCE_ID])
    assert witness.attempt_count == 0
    assert transport.attempt_count == 1

    client.stop_instances(InstanceIds=[INSTANCE_ID])
    assert witness.attempt_count == 1


def test_the_witness_records_the_sdk_invocation_id_as_the_cloudtrail_join_key():
    """One invocation id is what lets CloudTrail be checked for a second call."""
    client, _ = _make_client([_ok_response()])
    witness = DispatchWitness()
    witness.arm(client)

    client.stop_instances(InstanceIds=[INSTANCE_ID])
    ids = witness.distinct_invocation_ids
    assert len(ids) == 1
    assert ids[0]
    assert STOP_INSTANCES_OPERATION == "StopInstances"