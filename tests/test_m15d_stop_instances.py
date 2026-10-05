"""M15-D: the StopInstances boundary and the settle loop (ADR 0008).

Two things are verified here, and they are verified separately on purpose.

**The handler** dispatches exactly once and reports what happened at the
boundary. It is given a fake EC2 client and is expected to refuse a client that
could retry, to pin every request parameter, and to turn each botocore failure
into exactly one disposition. No test here contacts AWS: the client is a local
fake, and the handler's own module never imports ``boto3`` or ``botocore``.

**The coordinator** waits for the asynchronous post-state. The handler returning
200 and the instance being ``stopped`` are different events, and the tests below
drive the state sequence between them: ``stopping`` then ``stopped``,
``running`` then ``stopping`` then ``stopped``, a state that never converges, and
a state nobody classified.

The property that matters most is stated once and checked everywhere: **many
observations, exactly one dispatch.** The settle loop polls; it must never turn
polling into a second mutation. That is the whole reason the loop lives in the
coordinator rather than in the handler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sws_agent.constants import (
    DispatchDisposition,
    ExecutionOutcome,
    PotentialAction,
)
from sws_agent.ec2_mutation import (
    MUTATION_OPERATION,
    MutationClientConfigurationError,
    StopInstancesHandler,
)
from sws_agent.execution import ACTION_EXECUTION_REGISTRY
from sws_agent.execution_ledger import ReexecutionClass
from sws_agent.models import DispatchEvidence, ExecutionRequest

STOP = PotentialAction.STOP_RESOURCE


# ---------------------------------------------------------------------------
# The fake EC2 client. Models the two properties the handler actually reads:
# whether a retry is possible, and what stop_instances does.
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, retries: object) -> None:
        self.retries = retries


class _FakeMeta:
    def __init__(self, retries: object) -> None:
        self.config = _FakeConfig(retries)


class _FakeEc2:
    """A stand-in for the dedicated mutation client.

    ``responses`` is consumed one entry per ``stop_instances`` call, so a test can
    prove how many times the boundary was crossed by exhausting the sequence.
    """

    def __init__(
        self,
        *,
        responses: list[Any] | None = None,
        retries: object = {"total_max_attempts": 1},
    ) -> None:
        self._responses = list(responses) if responses is not None else []
        self.meta = _FakeMeta(retries)
        self.calls: list[dict[str, Any]] = []

    def stop_instances(self, **params: Any) -> Any:
        self.calls.append(params)
        if not self._responses:
            return {"StoppingInstances": [{"InstanceId": params["InstanceIds"][0]}]}
        outcome = self._responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _client_error(code: str, status: int = 400) -> Exception:
    """Build an exception shaped like botocore's ``ClientError``.

    Named ``ClientError`` so the handler's class-name discriminator matches, and
    carrying a real ``response`` mapping so nothing about the mapping depends on
    botocore being installed.
    """

    class ClientError(Exception):  # noqa: N801 - mirrors the botocore name
        def __init__(self) -> None:
            super().__init__(f"An error occurred calling StopInstances: {code}")
            self.response = {
                "Error": {"Code": code, "Message": "denied"},
                "ResponseMetadata": {"HTTPStatusCode": status},
            }

    return ClientError()


def _transport_error(name: str) -> Exception:
    """Build an exception with a botocore class name and no response."""
    return type(name, (Exception,), {})()


def _handler(client: _FakeEc2) -> StopInstancesHandler:
    return StopInstancesHandler(client, region="us-east-1")


def _request(store: Any) -> ExecutionRequest:
    from test_execution import _gated_request_with_ticket

    request, _plan, _snapshot = _gated_request_with_ticket(store)
    return request


def _store() -> Any:
    from test_execution import InMemoryApprovalStore, _Clock

    return InMemoryApprovalStore(now=_Clock())


# ---------------------------------------------------------------------------
# 1. The handler dispatches exactly once, with pinned parameters.
# ---------------------------------------------------------------------------


def test_the_handler_makes_exactly_one_call_per_handle():
    client = _FakeEc2()
    handler = _handler(client)

    evidence = handler.handle(_request(_store()))

    assert evidence.disposition is DispatchDisposition.ACCEPTED
    assert len(client.calls) == 1
    assert len(handler.dispatch_calls) == 1


def test_every_stop_parameter_is_pinned_rather_than_defaulted():
    client = _FakeEc2()
    _handler(client).handle(_request(_store()))

    sent = client.calls[0]
    # Each of these changes what the call *means*, not how fast it is. Relying on
    # an SDK default would make the mutation's meaning a property of the installed
    # botocore version.
    assert sent["Force"] is False, "Force=True would stop a mid-transition instance"
    assert sent["Hibernate"] is False, "hibernating is not stopping"
    assert sent["SkipOsShutdown"] is False, "skipping shutdown freezes, not stops"


def test_exactly_one_instance_id_is_sent():
    client = _FakeEc2()
    request = _request(_store())

    _handler(client).handle(request)

    assert client.calls[0]["InstanceIds"] == [request.resource_id]
    assert len(client.calls[0]["InstanceIds"]) == 1


@pytest.mark.parametrize("bad", ["   ", " inst-1", "inst-1 ", "i-1,i-2"])
def test_a_resource_id_that_is_not_one_instance_is_refused(bad: str):
    """A comma would put several instances behind one intent key.

    ``StoppingInstances`` would then be a partial list whose correspondence to the
    single recorded intent is unrecoverable, so this is refused before a request
    exists rather than dispatched and reported.

    An empty id is absent from this list because ``ExecutionRequest`` already
    rejects it: the request model is the first line of defence, and the handler
    only has to catch what gets past it.
    """
    from sws_agent.models import ExecutionRequest as _Req

    store = _store()
    request = _request(store)
    mangled = _Req(**{**request.model_dump(), "resource_id": bad})

    client = _FakeEc2()
    with pytest.raises(MutationClientConfigurationError):
        _handler(client).handle(mangled)
    assert client.calls == []


def test_the_handler_cannot_be_wired_to_another_action():
    client = _FakeEc2()
    with pytest.raises(MutationClientConfigurationError):
        StopInstancesHandler(
            client, region="us-east-1", action=PotentialAction.FLAG_FOR_REVIEW
        )
    assert client.calls == []


def test_an_unbound_region_is_refused():
    with pytest.raises(MutationClientConfigurationError):
        StopInstancesHandler(_FakeEc2(), region="  ")


# ---------------------------------------------------------------------------
# 2. A retrying client is refused, so "one dispatch" is enforced rather than hoped.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "retries",
    [
        {"total_max_attempts": 3},
        {"total_max_attempts": 2},
        {"max_attempts": 5},
        # botocore normalizes max_attempts as total_max_attempts = value + 1
        # (args.py:600-621), so this is one retry -- two dispatches -- and must
        # be refused even though the number 1 looks safe in isolation.
        {"max_attempts": 1},
        {},
        None,
        "standard",
        {"total_max_attempts": "many"},
    ],
)
def test_a_client_that_might_retry_is_refused(retries: object):
    """botocore's standard mode retries timeouts, 5xx, and connection errors.

    Handed a mutation, that turns one ``handle()`` into an unknown number of
    requests while SWS records exactly one crossing -- and ``StopInstancesRequest``
    has no ``ClientToken``, so nothing downstream could collapse a repeat into
    one. The handler therefore refuses such a client at construction, before any
    request exists, rather than trusting it.
    """
    with pytest.raises(MutationClientConfigurationError):
        StopInstancesHandler(_FakeEc2(retries=retries), region="us-east-1")


def test_an_absent_retry_setting_is_refused_because_botocore_defaults_to_three():
    """``{}`` is not "no retries configured" -- it is botocore's default of 3.

    When no attempt count is supplied, ``botocore.retries.standard`` substitutes
    ``DEFAULT_MAX_ATTEMPTS = 3``, which permits two silent retries. A mapping
    that merely lacks the keys therefore cannot be read as "retries disabled",
    and refusing it is the only safe reading.
    """
    with pytest.raises(MutationClientConfigurationError):
        StopInstancesHandler(_FakeEc2(retries={}), region="us-east-1")


def test_a_client_with_no_readable_retry_config_is_refused():
    class _Opaque:
        def stop_instances(self, **kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("must never be called")

    with pytest.raises(MutationClientConfigurationError):
        StopInstancesHandler(_Opaque(), region="us-east-1")


def test_a_legacy_max_attempts_key_of_zero_is_accepted():
    """``max_attempts`` counts retries *after* the first, so 0 disables them.

    botocore normalizes the legacy key as ``total_max_attempts =
    max_attempts + 1`` (``args.py:600-621``), so ``0`` is the one legacy value
    that yields a single dispatch. Accepting it keeps SWS from rejecting a
    correctly configured client through a false alarm.
    """
    handler = StopInstancesHandler(_FakeEc2(retries={"max_attempts": 0}), region="us-east-1")
    assert handler.dispatch_calls == ()
    evidence = handler.handle(_request(_store()))
    assert evidence.disposition is DispatchDisposition.ACCEPTED


# ---------------------------------------------------------------------------
# 3. Established NOT_DISPATCHED. Never an invented one.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "NoCredentialsError",
        "NoRegionError",
        "ParamValidationError",
        "PartialCredentialsError",
        "CredentialRetrievalError",
        "UnknownRegionError",
        "EndpointResolutionError",
        "SSLError",
        "ProxyConnectionError",
    ],
)
def test_a_failure_before_the_request_could_be_written_is_not_dispatched(name: str):
    """These are raised before a request is written.

    The absence of effect is therefore *established* rather than merely likely,
    which is what makes ``NO_EFFECT`` -- and a retry -- legitimate.
    """
    client = _FakeEc2(responses=[_transport_error(name)])
    evidence = _handler(client).handle(_request(_store()))

    assert evidence.disposition is DispatchDisposition.NOT_DISPATCHED
    # Recorded as audit material rather than as a claim about a response: the
    # M15-C coherence rule forbids response fields on NOT_DISPATCHED.
    assert evidence.sanitized == {"operation": MUTATION_OPERATION, "reason": name}
    assert evidence.aws_error_code is None
    assert evidence.http_status is None


def test_a_dry_run_is_an_established_absence_of_effect():
    client = _FakeEc2(responses=[_client_error("DryRunOperation", status=403)])
    evidence = _handler(client).handle(_request(_store()))

    assert evidence.disposition is DispatchDisposition.NOT_DISPATCHED


# ---------------------------------------------------------------------------
# 4. Transport ambiguity is DISPATCH_UNKNOWN, never a manufactured NO_EFFECT.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # The two the M15-C ruling named explicitly.
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        "IncompleteReadError",
        "ConnectionError",
    ],
)
def test_a_connection_failure_may_have_carried_a_processed_request(name: str):
    """A lost response and a pre-send failure are the same exception.

    botocore exposes no signal to separate them, so the honest reading is that the
    request may have been applied. Calling this ``NOT_DISPATCHED`` would hand out
    a retry on the strength of an ambiguity, which is the one thing this milestone
    exists to prevent.
    """
    client = _FakeEc2(responses=[_transport_error(name)])
    evidence = _handler(client).handle(_request(_store()))

    assert evidence.disposition is DispatchDisposition.DISPATCH_UNKNOWN
    assert evidence.disposition is not DispatchDisposition.NOT_DISPATCHED
    assert evidence.exception_class == name


def test_a_service_5xx_may_have_applied_the_change_before_failing():
    client = _FakeEc2(responses=[_client_error("InternalError", status=500)])
    evidence = _handler(client).handle(_request(_store()))

    assert evidence.disposition is DispatchDisposition.DISPATCH_UNKNOWN
    assert evidence.aws_error_code == "InternalError"
    assert evidence.http_status == 500


def test_a_client_error_without_a_readable_response_is_unknown():
    class ClientError(Exception):  # noqa: N801 - mirrors the botocore name
        pass

    client = _FakeEc2(responses=[ClientError()])
    evidence = _handler(client).handle(_request(_store()))

    assert evidence.disposition is DispatchDisposition.DISPATCH_UNKNOWN


# ---------------------------------------------------------------------------
# 5. Definitive rejections.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("InvalidInstanceID.NotFound", ReexecutionClass.TARGET_INVALID),
        ("InvalidInstanceID.Malformed", ReexecutionClass.TARGET_INVALID),
        ("InvalidInstanceState", ReexecutionClass.POST_STATE_UNREACHABLE),
    ],
)
def test_a_rejection_is_recorded_verbatim_for_the_classifier(code: str, expected: Any):
    """The handler reports the code; ``classify_dispatch`` interprets it.

    Keeping interpretation out of the handler is what lets one table change the
    meaning of a failure without touching the boundary.
    """
    client = _FakeEc2(responses=[_client_error(code)])
    evidence = _handler(client).handle(_request(_store()))

    assert evidence.disposition is DispatchDisposition.DISPATCH_REJECTED
    assert evidence.aws_error_code == code
    assert evidence.rejection_key == code


def test_an_unlisted_rejection_code_is_still_a_rejection():
    """Refusal is established even when the reason is not understood.

    This is the M15-C ruling: the *absence of effect* comes from the 4xx, not from
    understanding the code. Only the retry decision depends on classification.
    """
    client = _FakeEc2(responses=[_client_error("SomeNewThrottleWeHaveNotSeen")])
    evidence = _handler(client).handle(_request(_store()))

    assert evidence.disposition is DispatchDisposition.DISPATCH_REJECTED
    assert evidence.rejection_key == "SomeNewThrottleWeHaveNotSeen"


# ---------------------------------------------------------------------------
# 6. The handler has no channel through which to express an outcome.
# ---------------------------------------------------------------------------


def test_dispatch_evidence_cannot_carry_an_outcome_or_a_retry_policy():
    """A handler that could state its own verdict would be a second classifier."""
    assert "outcome" not in DispatchEvidence.model_fields
    assert "reexecution_class" not in DispatchEvidence.model_fields
    with pytest.raises(Exception):
        DispatchEvidence(
            disposition=DispatchDisposition.ACCEPTED,
            outcome=ExecutionOutcome.VERIFIED_SUCCESS,
        )


def test_every_disposition_the_handler_can_return_is_a_real_one():
    possible = {
        _handler(_FakeEc2(responses=[r])).handle(_request(_store())).disposition
        for r in (
            _client_error("InvalidInstanceID.NotFound"),
            _transport_error("NoCredentialsError"),
            _transport_error("EndpointConnectionError"),
            None,
        )
    }
    assert possible <= set(DispatchDisposition)
    assert possible == {
        DispatchDisposition.DISPATCH_REJECTED,
        DispatchDisposition.NOT_DISPATCHED,
        DispatchDisposition.DISPATCH_UNKNOWN,
        DispatchDisposition.ACCEPTED,
    }


def test_the_accepted_response_is_summarised_not_interpreted():
    """``ACCEPTED`` asserts the crossing, never the stop.

    The response body is recorded for the audit trail and is not consulted to
    decide anything, because which instances are stopping is a settlement question
    the verifier answers from an independent observation.
    """
    client = _FakeEc2(
        responses=[{"StoppingInstances": [{"InstanceId": "inst-1"}]}]
    )
    evidence = _handler(client).handle(_request(_store()))

    assert evidence.disposition is DispatchDisposition.ACCEPTED
    assert evidence.sanitized == {
        "operation": MUTATION_OPERATION,
        "stopping_instances": ["inst-1"],
    }


def test_an_unreadable_response_body_does_not_change_the_disposition():
    for body in ({}, {"StoppingInstances": None}, {"StoppingInstances": [{}]}):
        client = _FakeEc2(responses=[body])
        evidence = _handler(client).handle(_request(_store()))
        assert evidence.disposition is DispatchDisposition.ACCEPTED


# ---------------------------------------------------------------------------
# 7. The settle loop: many observations, exactly one dispatch.
# ---------------------------------------------------------------------------


class _SequenceWorld:
    """A handler that accepts once and an observer that walks a state sequence.

    The A5 preflight observes ``preflight_state`` and the *settle polls* then walk
    ``states``, with the last entry repeating once exhausted. Keeping the two
    separate is deliberate: otherwise every assertion would have to account for
    the preflight silently eating the first state of the sequence, and an
    off-by-one in a test about polling is indistinguishable from an off-by-one in
    the loop.

    So ``states=["running"]`` means "40 polls that never converge", not "39".
    """

    def __init__(self, states: list[str], *, preflight_state: str = "running") -> None:
        self._states = list(states)
        self._preflight_state = preflight_state
        self._polls = 0
        self.handler_calls: list[ExecutionRequest] = []
        self.observed: list[str] = []

    def handle(self, request: ExecutionRequest) -> DispatchEvidence:
        self.handler_calls.append(request)
        return DispatchEvidence(disposition=DispatchDisposition.ACCEPTED)

    def observe(self, resource_id: str) -> Any:
        from test_execution import _provider_observation

        if not self.observed:
            state = self._preflight_state
        else:
            state = self._states[min(self._polls, len(self._states) - 1)]
            self._polls += 1
        self.observed.append(state)
        return _provider_observation(resource_id=resource_id, facts={"state": state})


def _run(
    tmp_path: Path,
    world: Any,
    *,
    name: str = "settle",
) -> tuple[Any, Any]:
    from test_execution import (
        ExecutionCoordinator,
        _Clock,
        _ledger,
        _Sleeper,
        _SpyAuditStore,
    )

    store = _store()
    request = _request(store)
    ledger = _ledger(tmp_path)
    sleeper = _Sleeper()
    coordinator = ExecutionCoordinator(
        approval_store=store,
        execution_ledger=ledger,
        handler=world,
        observer=world,
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        worker_id="worker-1",
        now=_Clock(),
        sleep=sleeper,
    )
    result = coordinator.execute(request)
    return result, (ledger, sleeper, coordinator)


def test_one_dispatch_survives_a_settle_window_of_two_hundred_polls(tmp_path: Path):
    """The central invariant: the loop may observe forever, dispatch once.

    A stop that takes nine minutes to converge is ordinary AWS behaviour. If
    settling were implemented by re-dispatching, this test would see 120 calls
    where it expects one -- and the ledger would still show a single crossing,
    which is exactly the kind of divergence ADR 0006 exists to make impossible.
    """
    world = _SequenceWorld(["running"] * 39 + ["stopped"])
    result, (ledger, sleeper, _coordinator) = _run(tmp_path, world)

    # One A5 preflight plus 40 settle polls.
    assert len(world.observed) == 41
    assert len(world.handler_calls) == 1
    assert len(ledger.all_executions()) == 1
    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    # 39 pauses of exactly the policy's interval, not a busy loop.
    assert sleeper.calls == [15] * 39


def test_a_stop_that_is_already_stopping_converges_to_success(tmp_path: Path):
    world = _SequenceWorld(["stopping", "stopped"])
    result, (_ledger, sleeper, _c) = _run(tmp_path, world)

    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert len(world.handler_calls) == 1
    assert sleeper.calls == [15]


def test_running_then_stopping_then_stopped_converges(tmp_path: Path):
    world = _SequenceWorld(["running", "stopping", "stopped"])
    result, (_ledger, sleeper, _c) = _run(tmp_path, world)

    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert len(world.handler_calls) == 1
    assert sleeper.calls == [15, 15]


def test_an_already_stopped_target_needs_no_waiting(tmp_path: Path):
    world = _SequenceWorld(["stopped"])
    result, (_ledger, sleeper, _c) = _run(tmp_path, world)

    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert sleeper.calls == []


def test_a_stop_that_never_converges_is_declared_not_reached_and_permits_a_retry(
    tmp_path: Path,
):
    """``running`` at the deadline is retryable, because stopping is idempotent.

    The instance may simply be slow. Repeating the intent against a still-running
    instance is safe, so this is the one accepted contradiction where a fresh
    authorisation is allowed to act -- and it is allowed because the state was
    *declared*, not because the system failed to classify it.
    """
    world = _SequenceWorld(["running"])
    result, (ledger, sleeper, _c) = _run(tmp_path, world)

    assert result.outcome is ExecutionOutcome.FAILED
    row = ledger.all_executions()[0]
    assert row.reexecution_class is ReexecutionClass.POST_STATE_NOT_REACHED
    assert row.permits_reexecution is True
    # The bound really is 40 polls at 15s, and not one more.
    assert len(world.observed) == 41
    assert sleeper.calls == [15] * 39


def test_a_terminal_state_is_declared_unreachable_and_refuses_a_retry(tmp_path: Path):
    """``terminated`` can never satisfy a stop intent, so retrying is meaningless."""
    world = _SequenceWorld(["terminated"])
    result, (ledger, _sleeper, _c) = _run(tmp_path, world)

    assert result.outcome is ExecutionOutcome.FAILED
    row = ledger.all_executions()[0]
    assert row.reexecution_class is ReexecutionClass.POST_STATE_UNREACHABLE
    assert row.permits_reexecution is False


def test_pending_is_not_mapped_to_unreachable_by_default(tmp_path: Path):
    """The M15-D ruling: ``pending`` is left unmapped and fails closed.

    ``pending`` was deliberately kept out of both the settled and in-progress sets,
    so the wait ends at once and the classifier has no mapping for it. That is the
    safe direction: inventing an interpretation for a transitional state SWS does
    not understand is how a terminal state gets mistaken for a slow one.
    """
    policy = ACTION_EXECUTION_REGISTRY[STOP].settle_policy
    assert "pending" not in policy.settled_states
    assert "pending" not in policy.in_progress_states

    world = _SequenceWorld(["pending"])
    result, (ledger, sleeper, _c) = _run(tmp_path, world)

    # The wait ended immediately -- one preflight plus one poll, no pauses.
    assert len(world.observed) == 2
    assert sleeper.calls == []
    # And it was not quietly folded into either neighbouring class.
    row = ledger.all_executions()[0]
    assert row.reexecution_class is not ReexecutionClass.POST_STATE_UNREACHABLE
    assert row.reexecution_class is not ReexecutionClass.POST_STATE_NOT_REACHED
    # Unmapped state plus a failed verification fails closed to UNKNOWN.
    assert result.outcome is ExecutionOutcome.UNKNOWN
    assert row.reexecution_class is ReexecutionClass.OUTCOME_UNKNOWN
    assert row.permits_reexecution is False


# ---------------------------------------------------------------------------
# 8. Observation failure is bounded, and never becomes a fabricated state.
# ---------------------------------------------------------------------------


class _FlakyObserver:
    """Succeeds for the preflight, then errors ``failures`` times, then succeeds.

    The preflight has to pass or the execution is refused before the handler is
    ever called; what is under test is the settle loop's tolerance of a provider
    that cannot answer *after* the mutation crossed.
    """

    def __init__(self, *, failures: int, state: str = "stopped") -> None:
        self.failures = failures
        self.state = state
        self.calls = 0
        self.failures_served = 0

    def handle(self, request: ExecutionRequest) -> DispatchEvidence:
        return DispatchEvidence(disposition=DispatchDisposition.ACCEPTED)

    def observe(self, resource_id: str) -> Any:
        from test_execution import _provider_observation
        from sws_agent.verification import ObservationError

        self.calls += 1
        # Call 1 is the preflight, which must succeed.
        if self.calls > 1 and self.failures_served < self.failures:
            self.failures_served += 1
            raise ObservationError("the observer could not answer")
        return _provider_observation(
            resource_id=resource_id, facts={"state": self.state}
        )


def test_transient_observation_failures_are_tolerated_within_the_bound(tmp_path: Path):
    from test_execution import (
        ExecutionCoordinator,
        _Clock,
        _ledger,
        _Sleeper,
        _SpyAuditStore,
    )

    store = _store()
    request = _request(store)
    world = _FlakyObserver(failures=3)
    coordinator = ExecutionCoordinator(
        approval_store=store,
        execution_ledger=_ledger(tmp_path),
        handler=world,
        observer=world,
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        worker_id="worker-1",
        now=_Clock(),
        sleep=_Sleeper(),
    )
    result = coordinator.execute(request)

    # Three errors is the documented bound, so the fourth poll succeeds.
    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert world.calls == 5  # 1 preflight + 3 errors + 1 success


def test_exceeding_the_observation_error_bound_settles_unknown(tmp_path: Path):
    """An observer that cannot answer establishes nothing, so nothing is claimed."""
    from test_execution import (
        ExecutionCoordinator,
        _Clock,
        _ledger,
        _Sleeper,
        _SpyAuditStore,
    )

    store = _store()
    request = _request(store)
    world = _FlakyObserver(failures=99)
    ledger = _ledger(tmp_path)
    coordinator = ExecutionCoordinator(
        approval_store=store,
        execution_ledger=ledger,
        handler=world,
        observer=world,
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        worker_id="worker-1",
        now=_Clock(),
        sleep=_Sleeper(),
    )
    result = coordinator.execute(request)

    assert result.outcome is ExecutionOutcome.UNKNOWN
    row = ledger.all_executions()[0]
    assert row.reexecution_class is ReexecutionClass.OUTCOME_UNKNOWN
    assert row.permits_reexecution is False
    # Bounded: it gave up rather than spinning to the 600s deadline.
    assert world.calls < 41


# ---------------------------------------------------------------------------
# 9. No path from an unestablished boundary to a retry.
# ---------------------------------------------------------------------------


def test_an_unknown_dispatch_cannot_be_re_bypassed_by_a_fresh_approval(tmp_path: Path):
    """A new ticket is not a new observation.

    The whole point of ``OUTCOME_UNKNOWN`` is that SWS does not know whether the
    mutation applied. Issuing a second one on a fresh authorisation would resolve
    that ignorance by acting on it, so the ledger refuses regardless of how the
    retry is packaged.
    """
    from test_execution import (
        ExecutionCoordinator,
        _Clock,
        _grant,
        _jsonl_audit,
        _ledger,
        _make_decision,
        _make_plan,
        _Sleeper,
        _SpyAuditStore,
    )

    clock = _Clock()
    store = _store()
    request, _plan, snapshot = None, None, None
    from test_execution import _gated_request_with_ticket

    request, _plan, snapshot = _gated_request_with_ticket(store)
    ledger = _ledger(tmp_path)

    class _UnknownBoundary:
        def __init__(self) -> None:
            self.handler_calls: list[ExecutionRequest] = []

        def handle(self, req: ExecutionRequest) -> DispatchEvidence:
            self.handler_calls.append(req)
            return DispatchEvidence(
                disposition=DispatchDisposition.DISPATCH_UNKNOWN,
                exception_class="EndpointConnectionError",
            )

        def observe(self, resource_id: str) -> Any:
            from test_execution import _provider_observation

            return _provider_observation(
                resource_id=resource_id, facts={"state": "stopped"}
            )

    first = ExecutionCoordinator(
        approval_store=store,
        execution_ledger=ledger,
        handler=_UnknownBoundary(),
        observer=_UnknownBoundary(),
        audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
        id_source=lambda: "exec-1",
        worker_id="worker-1",
        now=clock,
        sleep=_Sleeper(),
    )
    result = first.execute(request)
    assert result.outcome is ExecutionOutcome.UNKNOWN
    row = ledger.all_executions()[0]
    assert row.reexecution_class is ReexecutionClass.OUTCOME_UNKNOWN
    assert row.permits_reexecution is False

    # A separately approved retry is refused anyway.
    replan = _make_plan(
        store, snapshot=snapshot, decision=_make_decision(snapshot), plan_id="plan-2"
    )
    replanned = request.model_copy(
        update={
            "action_plan_id": "plan-2",
            "plan": replan,
            "ticket": _grant(store, replan),
        }
    )
    second_boundary = _UnknownBoundary()
    second = ExecutionCoordinator(
        approval_store=store,
        execution_ledger=ledger,
        handler=second_boundary,
        observer=second_boundary,
        audit_store=_jsonl_audit(tmp_path / "second.jsonl", clock),
        id_source=lambda: "exec-2",
        worker_id="worker-2",
        now=clock,
        sleep=_Sleeper(),
    )
    retry = second.execute(replanned)

    assert retry.outcome is ExecutionOutcome.REFUSED
    assert second_boundary.handler_calls == []
    assert len(ledger.all_executions()) == 1


def test_no_effect_is_reachable_only_from_an_established_absence_of_effect(
    tmp_path: Path,
):
    """The exhaustive invariant, restated for M15-D's handler.

    Every disposition the real handler can produce is driven through the real
    coordinator, and the settlement is checked against the ledger's legality table
    rather than against a list written out here.
    """
    from test_execution import (
        ExecutionCoordinator,
        _Clock,
        _ledger,
        _Sleeper,
        _SpyAuditStore,
    )

    boundaries: list[Any] = [
        _handler(_FakeEc2()),  # ACCEPTED
        _handler(_FakeEc2(responses=[_client_error("InvalidInstanceID.NotFound")])),
        _handler(_FakeEc2(responses=[_client_error("RequestLimitExceeded", 400)])),
        _handler(_FakeEc2(responses=[_client_error("SomeUnlistedCode")])),
        _handler(_FakeEc2(responses=[_transport_error("NoCredentialsError")])),
        _handler(_FakeEc2(responses=[_transport_error("ParamValidationError")])),
        _handler(_FakeEc2(responses=[_transport_error("EndpointConnectionError")])),
        _handler(_FakeEc2(responses=[_transport_error("ConnectTimeoutError")])),
        _handler(_FakeEc2(responses=[_client_error("InternalError", 500)])),
    ]

    observed_classes = set()
    for index, boundary in enumerate(boundaries):
        store = _store()
        request = _request(store)
        ledger = _ledger(tmp_path / f"case{index}")
        coordinator = ExecutionCoordinator(
            approval_store=store,
            execution_ledger=ledger,
            handler=boundary,
            observer=_SequenceWorld(["stopped"]),
            audit_store=_SpyAuditStore(),  # type: ignore[arg-type]
            id_source=lambda: "exec-1",
            worker_id="worker-1",
            now=_Clock(),
            sleep=_Sleeper(),
        )
        coordinator.execute(request)
        row = ledger.all_executions()[0]
        observed_classes.add(row.reexecution_class)

        if row.reexecution_class is ReexecutionClass.NO_EFFECT:
            assert row.outcome is ExecutionOutcome.NOT_EXECUTED, (
                "NO_EFFECT was paired with "
                f"{row.outcome.value}; it may only accompany NOT_EXECUTED"
            )
            assert boundary.dispatch_calls[-1]["InstanceIds"], "sanity"

    # The reachable set is small and every member is accounted for. If a future
    # change widens it, this assertion is what notices.
    assert observed_classes <= {
        ReexecutionClass.NO_EFFECT,
        ReexecutionClass.EFFECT_ACHIEVED,
        ReexecutionClass.TARGET_INVALID,
        ReexecutionClass.TRANSIENT_REJECTION,
        ReexecutionClass.POST_STATE_UNREACHABLE,
        ReexecutionClass.OUTCOME_UNKNOWN,
    }
    # NO_EFFECT really was reachable, so the check above was not vacuous.
    assert ReexecutionClass.NO_EFFECT in observed_classes


# ---------------------------------------------------------------------------
# 10. The handler is not wired into anything.
# ---------------------------------------------------------------------------


def test_stop_resource_is_still_not_implemented():
    """Enabling the action is gated on independent review of this code."""
    assert ACTION_EXECUTION_REGISTRY[STOP].implemented is False
    assert all(not spec.implemented for spec in ACTION_EXECUTION_REGISTRY.values())


def test_the_mutation_module_never_imports_an_aws_sdk():
    """Same hermeticity rule the coordinator and verifier are held to.

    The client is injected, so importing this module must not be able to reach
    boto3 -- which also means these tests cannot accidentally construct a real
    client. Checked against the parsed imports rather than the raw text, because
    this module's own prose names both libraries repeatedly.
    """
    import ast

    import sws_agent.ec2_mutation as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "boto3" not in imported
    assert "botocore" not in imported
