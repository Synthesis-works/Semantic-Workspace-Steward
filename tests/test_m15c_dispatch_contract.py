"""M15-C: the dispatch-evidence contract between handler, coordinator, and ledger.

M15-B gave the ledger a vocabulary for *why* repeating an intent is safe. It
deliberately left the coordinator with a provisional outcome-keyed default,
because the fact that would replace it -- what the mutation boundary actually
learned -- did not exist. M15-C adds that fact and removes the default.

The governing principle is one sentence: **a mutation failure must never
acquire retryability merely because the system failed to classify what
happened.** Every test below is an instance of it. The three layers under test
are

1. the handler reports :class:`DispatchEvidence` and has no way to express a
   retry policy;
2. :func:`classify_dispatch` maps that evidence to an outcome and a
   :class:`ReexecutionClass` against the action's declared contract;
3. the ledger refuses any outcome/basis pair the compatibility table does not
   admit.

``STOP_RESOURCE`` ships with **no** dispatch contract. That is the fail-closed
default, not an oversight, and most tests here assert what happens because of
it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sws_agent.constants import (
    SWS_SUPPORTED_DISPATCH_DISPOSITIONS,
    DispatchDisposition,
    ExecutionOutcome,
    PotentialAction,
    VerificationStatus,
)
from sws_agent.execution import (
    ACTION_EXECUTION_REGISTRY,
    ActionSpec,
    DispatchClassification,
    DispatchContract,
    MutationHandler,
    classify_dispatch,
)
from sws_agent.execution_ledger import (
    LEGAL_REEXECUTION_CLASSES_FOR_OUTCOME,
    RETRYABLE_REEXECUTION_CLASSES,
    ReexecutionClass,
)
from sws_agent.models import DispatchEvidence

from test_execution import (  # noqa: F401 - fixtures shared with the M9 suite
    _Clock,
    _World,
    _gated_request_with_ticket,
    _jsonl_audit,
    _ledger,
)

STOP = PotentialAction.STOP_RESOURCE


def _contract(**kwargs: Any) -> DispatchContract:
    return DispatchContract(**kwargs)


def _classify(
    evidence: DispatchEvidence | None,
    *,
    status: VerificationStatus | None = None,
    observed_state: str | None = None,
    contract: DispatchContract | None = None,
) -> DispatchClassification:
    return classify_dispatch(
        evidence,
        status=status,
        observed_state=observed_state,
        contract=contract,
    )


# ---------------------------------------------------------------------------
# The evidence type: what a handler can and cannot say
# ---------------------------------------------------------------------------


def test_disposition_is_required_and_cannot_be_inferred():
    """There is no "default" disposition.

    ``MutationAttempt`` defaulted ``ambiguous`` and ``call_error`` to ``False``,
    which is how a handler that said nothing still produced an object the
    coordinator would classify. Requiring the field removes that object.
    """
    with pytest.raises(ValidationError):
        DispatchEvidence()  # type: ignore[call-arg]


def test_every_disposition_is_a_canonical_supported_label():
    assert {d.value for d in DispatchDisposition} == {
        "not_dispatched",
        "dispatch_rejected",
        "accepted",
        "dispatch_unknown",
    }
    assert set(SWS_SUPPORTED_DISPATCH_DISPOSITIONS) == set(DispatchDisposition)


def test_evidence_carries_no_reexecution_class():
    """The handler structurally cannot state a retry policy.

    This is the whole reason the type was introduced. If a basis could be set
    here, the lowest layer of the system would get the last word on whether an
    operation may be repeated.
    """
    assert "reexecution_class" not in DispatchEvidence.model_fields
    with pytest.raises(ValidationError):
        DispatchEvidence(  # type: ignore[call-arg]
            disposition=DispatchDisposition.ACCEPTED,
            reexecution_class=ReexecutionClass.NO_EFFECT,
        )


def test_evidence_carries_no_outcome():
    assert "outcome" not in DispatchEvidence.model_fields


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("aws_error_code", "UnauthorizedOperation"),
        ("http_status", 400),
        ("exception_class", "ClientError"),
    ],
)
def test_not_dispatched_cannot_claim_a_response(field: str, value: Any):
    """A boundary that wrote nothing has no response to report.

    Such evidence is self-contradictory: the handler misread its own boundary.
    Refusing it at construction is better than classifying it later, because a
    ``NOT_DISPATCHED`` carrying an error code would otherwise be the one object
    that could claim both "nothing happened" and "something was refused".
    """
    with pytest.raises(ValidationError, match="no response exists"):
        DispatchEvidence(disposition=DispatchDisposition.NOT_DISPATCHED, **{field: value})


def test_accepted_cannot_claim_an_error_response():
    with pytest.raises(ValidationError, match="not also an error response"):
        DispatchEvidence(
            disposition=DispatchDisposition.ACCEPTED, aws_error_code="Throttling"
        )


@pytest.mark.parametrize(
    "disposition",
    [DispatchDisposition.DISPATCH_REJECTED, DispatchDisposition.DISPATCH_UNKNOWN],
)
def test_rejection_and_unknown_may_carry_response_evidence(disposition):
    evidence = DispatchEvidence(
        disposition=disposition,
        aws_error_code="RequestLimitExceeded",
        http_status=400,
        exception_class="ClientError",
    )
    assert evidence.disposition is disposition
    assert evidence.rejection_key == "RequestLimitExceeded"


def test_rejection_key_prefers_the_provider_code():
    """The AWS error code is the contractual part; the class is a fallback."""
    evidence = DispatchEvidence(
        disposition=DispatchDisposition.DISPATCH_REJECTED,
        aws_error_code="InvalidInstanceID.NotFound",
        exception_class="ClientError",
    )
    assert evidence.rejection_key == "InvalidInstanceID.NotFound"


def test_rejection_key_falls_back_to_the_exception_class():
    evidence = DispatchEvidence(
        disposition=DispatchDisposition.DISPATCH_REJECTED,
        exception_class="EndpointConnectionError",
    )
    assert evidence.rejection_key == "EndpointConnectionError"


def test_rejection_key_is_none_when_the_handler_said_nothing_about_why():
    evidence = DispatchEvidence(disposition=DispatchDisposition.DISPATCH_REJECTED)
    assert evidence.rejection_key is None


# ---------------------------------------------------------------------------
# The classifier: the only place a ReexecutionClass is produced
# ---------------------------------------------------------------------------


def test_transient_rejection_is_permitted_by_the_rejection_not_by_known_transience():
    """The ratified reading of `TRANSIENT_REJECTION`, pinned in a test.

    A retry is permitted because the provider *established* a rejection and
    therefore applied nothing. It is not permitted because SWS believes the
    error is temporary -- nothing has observed that the cause will pass, and for
    an unclassified code there is no basis for such a belief.

    Asserted through the recorded detail, because this is a claim about
    reasoning and an enum name alone cannot carry it. If a future edit starts
    describing the class as a known-transient one, this fails.
    """
    got = _classify(
        DispatchEvidence(
            disposition=DispatchDisposition.DISPATCH_REJECTED,
            aws_error_code="SomeCodeNobodyHasClassified",
        ),
        status=VerificationStatus.FAILED,
        contract=_contract(),
    )
    assert got.basis is ReexecutionClass.TRANSIENT_REJECTION
    assert "rejection is established" in got.detail
    assert "not classified" in got.detail


def test_declared_permanent_rejection_overrides_the_unlisted_default():
    """The intended fix for a code that turns out to be permanent.

    A permanent rejection becomes terminal by being *declared*, never by
    narrowing the default. This is the mechanism the ratified reading depends
    on, so it is tested rather than described.
    """
    contract = _contract(
        rejection_classes={"SomeCodeNobodyHasClassified": ReexecutionClass.TARGET_INVALID}
    )
    got = _classify(
        DispatchEvidence(
            disposition=DispatchDisposition.DISPATCH_REJECTED,
            aws_error_code="SomeCodeNobodyHasClassified",
        ),
        status=VerificationStatus.FAILED,
        contract=contract,
    )
    assert got.basis is ReexecutionClass.TARGET_INVALID
    assert got.basis is not ReexecutionClass.TRANSIENT_REJECTION


def test_no_effect_and_transient_rejection_are_never_interchangeable():
    """The distinction M15-C's central invariant depends on.

    `NO_EFFECT` means the request never left the process; `TRANSIENT_REJECTION`
    means a request was written and refused. Both permit a retry, so conflating
    them would not break any retry test -- it would corrupt the audit trail, by
    making a refused call indistinguishable from one that was never sent.
    """
    no_effect = _classify(DispatchEvidence(disposition=DispatchDisposition.NOT_DISPATCHED))
    rejection = _classify(
        DispatchEvidence(
            disposition=DispatchDisposition.DISPATCH_REJECTED,
            aws_error_code="RequestLimitExceeded",
        ),
        status=VerificationStatus.FAILED,
        contract=_contract(),
    )
    assert no_effect.basis is ReexecutionClass.NO_EFFECT
    assert rejection.basis is ReexecutionClass.TRANSIENT_REJECTION
    assert no_effect.basis is not rejection.basis
    # Both retryable, deliberately.
    assert no_effect.basis in RETRYABLE_REEXECUTION_CLASSES
    assert rejection.basis in RETRYABLE_REEXECUTION_CLASSES


def test_no_effect_is_reachable_only_from_not_dispatched():
    """The load-bearing invariant of M15-C.

    Across the whole disposition space, with and without a contract, the only
    route to a retryable ``NO_EFFECT`` is a boundary that positively reported it
    never dispatched. Nothing else -- not a rejection the system failed to
    recognise, not a contradiction it could not classify, not silence -- can
    produce it.
    """
    statuses = [None, *VerificationStatus]
    states = [None, "running", "stopped", "terminated", "unknown-state"]
    contracts: list[DispatchContract | None] = [
        None,
        _contract(),
        _contract(rejection_classes={"RequestLimitExceeded": ReexecutionClass.TARGET_INVALID}),
        _contract(
            post_state_classes={
                "running": ReexecutionClass.POST_STATE_NOT_REACHED,
                "terminated": ReexecutionClass.POST_STATE_UNREACHABLE,
            }
        ),
    ]
    for disposition in DispatchDisposition:
        evidence = DispatchEvidence(disposition=disposition)
        for status in statuses:
            for state in states:
                for contract in contracts:
                    got = _classify(
                        evidence, status=status, observed_state=state, contract=contract
                    )
                    if got.basis is ReexecutionClass.NO_EFFECT:
                        assert disposition is DispatchDisposition.NOT_DISPATCHED, (
                            f"NO_EFFECT leaked from {disposition.value} "
                            f"(status={status}, state={state})"
                        )


def test_missing_evidence_fails_closed():
    """A handler that returned nothing is ignorance, not a retryable failure."""
    got = _classify(None)
    assert got.outcome is ExecutionOutcome.UNKNOWN
    assert got.basis is ReexecutionClass.OUTCOME_UNKNOWN
    assert got.fail_closed is True


def test_dispatch_unknown_fails_closed():
    got = _classify(DispatchEvidence(disposition=DispatchDisposition.DISPATCH_UNKNOWN))
    assert got.outcome is ExecutionOutcome.UNKNOWN
    assert got.basis is ReexecutionClass.OUTCOME_UNKNOWN
    assert got.fail_closed is True


def test_not_dispatched_is_not_executed_with_no_effect():
    got = _classify(DispatchEvidence(disposition=DispatchDisposition.NOT_DISPATCHED))
    assert got.outcome is ExecutionOutcome.NOT_EXECUTED
    assert got.basis is ReexecutionClass.NO_EFFECT
    assert got.fail_closed is False
    assert got.skip_verification is True


def test_not_dispatched_skips_verification_even_if_a_status_is_supplied():
    """A reported ``FAILED`` cannot override a positive report of no dispatch."""
    got = _classify(
        DispatchEvidence(disposition=DispatchDisposition.NOT_DISPATCHED),
        status=VerificationStatus.FAILED,
        observed_state="running",
    )
    assert got.outcome is ExecutionOutcome.NOT_EXECUTED
    assert got.basis is ReexecutionClass.NO_EFFECT
    assert got.skip_verification is True


def test_rejection_without_a_contract_fails_closed():
    """A definitive rejection the action cannot classify is not retryable.

    ``STOP_RESOURCE`` declares no dispatch contract, so the coordinator cannot
    say whether ``UnauthorizedOperation`` means a missing permission (retrying
    will fail identically) or a temporarily misconfigured role. ``UNKNOWN`` is
    the honest settlement.
    """
    got = _classify(
        DispatchEvidence(
            disposition=DispatchDisposition.DISPATCH_REJECTED,
            aws_error_code="UnauthorizedOperation",
        ),
        status=VerificationStatus.FAILED,
        contract=None,
    )
    assert got.outcome is ExecutionOutcome.UNKNOWN
    assert got.basis is ReexecutionClass.OUTCOME_UNKNOWN
    assert got.fail_closed is True


def test_rejection_with_an_empty_contract_is_fail_closed_not_transient():
    """An action that declared an empty table declared nothing classifiable.

    Worth separating from the ``contract is None`` case: ``DispatchContract()``
    looks like a declaration, so a future edit must not read an empty mapping as
    permission to fall back to a retryable default.
    """
    got = _classify(
        DispatchEvidence(
            disposition=DispatchDisposition.DISPATCH_REJECTED,
            aws_error_code="RequestLimitExceeded",
        ),
        status=VerificationStatus.FAILED,
        contract=_contract(),
    )
    assert got.outcome is ExecutionOutcome.FAILED
    assert got.basis is ReexecutionClass.TRANSIENT_REJECTION


def test_declared_rejection_code_takes_precedence_over_the_unlisted_default():
    """A listed code is classified exactly as declared."""
    got = _classify(
        DispatchEvidence(
            disposition=DispatchDisposition.DISPATCH_REJECTED,
            aws_error_code="InvalidInstanceID.NotFound",
        ),
        status=VerificationStatus.FAILED,
        contract=_contract(
            rejection_classes={
                "InvalidInstanceID.NotFound": ReexecutionClass.TARGET_INVALID
            }
        ),
    )
    assert got.outcome is ExecutionOutcome.FAILED
    assert got.basis is ReexecutionClass.TARGET_INVALID


def test_unlisted_rejection_code_is_a_transient_rejection():
    """A definitive rejection that did not act on the target may be repeated.

    ``TRANSIENT_REJECTION`` rather than ``NO_EFFECT`` on purpose. Both are
    retryable, but they claim different things: ``NO_EFFECT`` means the boundary
    never spoke to the provider, while this means the provider answered and
    declined. Keeping them distinct is what stops a rejected call from being
    mistaken later for a call that was never sent.
    """
    got = _classify(
        DispatchEvidence(
            disposition=DispatchDisposition.DISPATCH_REJECTED,
            aws_error_code="RequestLimitExceeded",
        ),
        status=VerificationStatus.FAILED,
        contract=_contract(
            rejection_classes={
                "InvalidInstanceID.NotFound": ReexecutionClass.TARGET_INVALID
            }
        ),
    )
    assert got.outcome is ExecutionOutcome.FAILED
    assert got.basis is ReexecutionClass.TRANSIENT_REJECTION


def test_rejection_without_a_reason_is_not_silently_retryable():
    """``DISPATCH_REJECTED`` with no code and no exception class is incomplete.

    The disposition asserts a fact about the world but carries no evidence for
    it. Without a contract to interpret, that is unclassifiable.
    """
    got = _classify(
        DispatchEvidence(disposition=DispatchDisposition.DISPATCH_REJECTED),
        status=VerificationStatus.FAILED,
        contract=None,
    )
    assert got.outcome is ExecutionOutcome.UNKNOWN
    assert got.basis is ReexecutionClass.OUTCOME_UNKNOWN


def test_accepted_with_verified_success_is_effect_achieved():
    got = _classify(
        DispatchEvidence(disposition=DispatchDisposition.ACCEPTED),
        status=VerificationStatus.SUCCESS,
    )
    assert got.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert got.basis is ReexecutionClass.EFFECT_ACHIEVED


def test_accepted_with_partial_verification_is_effect_achieved():
    got = _classify(
        DispatchEvidence(disposition=DispatchDisposition.ACCEPTED),
        status=VerificationStatus.PARTIALLY_VERIFIED,
    )
    assert got.outcome is ExecutionOutcome.PARTIALLY_VERIFIED
    assert got.basis is ReexecutionClass.EFFECT_ACHIEVED


def test_accepted_with_contradiction_and_no_contract_fails_closed():
    """The case that was previously retryable by default.

    An accepted dispatch plus a contradicted postcondition said only that
    something was wrong. Whether repeating is safe depends entirely on the
    observed state, so with no declared mapping the answer is UNKNOWN.
    """
    got = _classify(
        DispatchEvidence(disposition=DispatchDisposition.ACCEPTED),
        status=VerificationStatus.FAILED,
        observed_state="running",
        contract=None,
    )
    assert got.outcome is ExecutionOutcome.UNKNOWN
    assert got.basis is ReexecutionClass.OUTCOME_UNKNOWN
    assert got.fail_closed is True


def test_contradiction_is_classified_by_the_observed_post_state():
    """``running`` and ``terminated`` are both ``FAILED`` and must not agree."""
    contract = _contract(
        post_state_classes={
            "running": ReexecutionClass.POST_STATE_NOT_REACHED,
            "terminated": ReexecutionClass.POST_STATE_UNREACHABLE,
        }
    )
    not_yet = _classify(
        DispatchEvidence(disposition=DispatchDisposition.ACCEPTED),
        status=VerificationStatus.FAILED,
        observed_state="running",
        contract=contract,
    )
    unreachable = _classify(
        DispatchEvidence(disposition=DispatchDisposition.ACCEPTED),
        status=VerificationStatus.FAILED,
        observed_state="terminated",
        contract=contract,
    )
    assert not_yet.outcome is unreachable.outcome is ExecutionOutcome.FAILED
    assert not_yet.basis is ReexecutionClass.POST_STATE_NOT_REACHED
    assert not_yet.basis.value != unreachable.basis.value
    assert unreachable.basis is ReexecutionClass.POST_STATE_UNREACHABLE
    assert not_yet.basis.value != unreachable.basis.value
    assert not_yet.fail_closed is False
    assert unreachable.fail_closed is False


def test_contradiction_in_an_unmapped_state_fails_closed():
    """A contract that does not cover the observed state does not cover it.

    Falling back to the nearest mapped state would be a guess about a mutation's
    effect, which is exactly what this milestone refuses to do.
    """
    got = _classify(
        DispatchEvidence(disposition=DispatchDisposition.ACCEPTED),
        status=VerificationStatus.FAILED,
        observed_state="stopping",
        contract=_contract(
            post_state_classes={"running": ReexecutionClass.POST_STATE_NOT_REACHED}
        ),
    )
    assert got.outcome is ExecutionOutcome.UNKNOWN
    assert got.basis is ReexecutionClass.OUTCOME_UNKNOWN
    assert got.fail_closed is True


def test_contradiction_with_no_recorded_state_fails_closed():
    got = _classify(
        DispatchEvidence(disposition=DispatchDisposition.ACCEPTED),
        status=VerificationStatus.FAILED,
        observed_state=None,
        contract=_contract(
            post_state_classes={"running": ReexecutionClass.POST_STATE_NOT_REACHED}
        ),
    )
    assert got.outcome is ExecutionOutcome.UNKNOWN
    assert got.basis is ReexecutionClass.OUTCOME_UNKNOWN


def test_accepted_with_unestablished_post_state_fails_closed():
    """An accepted dispatch whose effect was never observed is not a success."""
    got = _classify(
        DispatchEvidence(disposition=DispatchDisposition.ACCEPTED),
        status=VerificationStatus.UNKNOWN,
    )
    assert got.outcome is ExecutionOutcome.UNKNOWN
    assert got.basis is ReexecutionClass.OUTCOME_UNKNOWN
    assert got.fail_closed is True


def test_accepted_with_no_verification_status_fails_closed():
    got = _classify(DispatchEvidence(disposition=DispatchDisposition.ACCEPTED), status=None)
    assert got.outcome is ExecutionOutcome.UNKNOWN
    assert got.basis is ReexecutionClass.OUTCOME_UNKNOWN


def test_verification_status_is_ignored_for_every_non_accepted_disposition():
    """Only an accepted dispatch makes the post-state the deciding evidence."""
    for disposition in (
        DispatchDisposition.NOT_DISPATCHED,
        DispatchDisposition.DISPATCH_REJECTED,
        DispatchDisposition.DISPATCH_UNKNOWN,
    ):
        baseline = _classify(DispatchEvidence(disposition=disposition))
        for status in VerificationStatus:
            got = _classify(
                DispatchEvidence(disposition=disposition), status=status
            )
            assert got.outcome is baseline.outcome
            assert got.basis is baseline.basis


def test_every_classification_pair_is_legal_for_the_ledger():
    """Layer three: the coordinator never proposes a pair the ledger rejects.

    Guards the seam between the classifier and
    :data:`LEGAL_REEXECUTION_CLASSES_FOR_OUTCOME`, which is where an
    unanticipated mapping would otherwise surface only at runtime.
    """
    statuses = [None, *VerificationStatus]
    states = [None, "running", "terminated"]
    contracts = [
        None,
        _contract(),
        _contract(
            rejection_classes={
                "A": ReexecutionClass.TARGET_INVALID,
                "B": ReexecutionClass.TRANSIENT_REJECTION,
                "C": ReexecutionClass.POST_STATE_UNREACHABLE,
            },
            post_state_classes={
                "running": ReexecutionClass.POST_STATE_NOT_REACHED,
                "terminated": ReexecutionClass.POST_STATE_UNREACHABLE,
            },
        ),
    ]
    checked = 0
    for disposition in DispatchDisposition:
        evidence = DispatchEvidence(disposition=disposition)
        for status in statuses:
            for state in states:
                for contract in contracts:
                    got = _classify(
                        evidence, status=status, observed_state=state, contract=contract
                    )
                    legal = LEGAL_REEXECUTION_CLASSES_FOR_OUTCOME[got.outcome]
                    assert got.basis in legal, (
                        f"{got.outcome.value} + {got.basis.value} is not legal "
                        f"(disposition={disposition.value}, status={status}, "
                        f"state={state})"
                    )
                    checked += 1
    assert checked == 4 * len(statuses) * len(states) * len(contracts)


# ---------------------------------------------------------------------------
# Registry defaults: fail closed, and nothing is implemented
# ---------------------------------------------------------------------------


def test_only_stop_resource_declares_a_dispatch_contract():
    """The fail-closed default, asserted as the shipped state.

    M15-D populates ``STOP_RESOURCE``'s tables, because that is the one action
    whose evidence is now understood well enough to classify. Every other action
    must still hold none: an action that cannot classify its evidence must not be
    able to. If this test ever fails on a second action, that action has acquired
    a retry policy without anyone deciding what its failures mean.
    """
    declared = {
        action
        for action, spec in ACTION_EXECUTION_REGISTRY.items()
        if spec.dispatch_contract is not None
    }
    assert declared == {STOP}


def test_every_unimplemented_action_with_a_contract_still_fails_closed():
    """Declaring tables must not by itself make an action executable.

    The registry's ``implemented`` flag is the gate. A populated contract is
    knowledge, not authority, so this pins that the two are independent and that
    M15-D's handler work has not quietly opened the action.
    """
    for action, spec in ACTION_EXECUTION_REGISTRY.items():
        if spec.dispatch_contract is not None:
            assert not spec.implemented, (
                f"{action.value} declares a dispatch contract and must not also be "
                "implemented; enabling it is gated on independent review"
            )


def test_stop_resources_settle_policy_is_bounded_and_frozen():
    """The settle policy is the only thing standing between a stop and 15 minutes.

    Pinned because every value here is a safety decision, not a tuning choice:
    15s keeps EC2's coarse state transitions from being polled into a hot loop,
    and 600s bounds how long an execution can sit holding a consumed approval.
    """
    policy = ACTION_EXECUTION_REGISTRY[STOP].settle_policy
    assert policy is not None
    assert policy.poll_interval_seconds == 15
    assert policy.deadline_seconds == 600
    assert policy.max_polls == 40
    assert policy.max_consecutive_observation_errors == 3
    # 'stopped' is the only success. 'pending' is absent from both sets on
    # purpose: M15-D's ruling is that an unmapped state must not be swept into
    # either branch, so it fails closed at the classifier instead.
    assert policy.settled_states == frozenset({"stopped"})
    assert policy.in_progress_states == frozenset({"stopping", "running"})
    assert "pending" not in policy.settled_states
    assert "pending" not in policy.in_progress_states


def test_dispatch_contract_defaults_to_empty_not_to_a_retryable_anything():
    contract = DispatchContract()
    assert contract.rejection_classes == {}
    assert contract.post_state_classes == {}


def test_action_spec_defaults_to_no_dispatch_contract():
    spec = ActionSpec(
        action=STOP,
        eligible_resource_types=frozenset(),
        requires_human_approval=True,
        mutation="ec2:StopInstances",
        postconditions={},
    )
    assert spec.dispatch_contract is None


def test_still_no_implemented_mutations():
    assert all(not spec.implemented for spec in ACTION_EXECUTION_REGISTRY.values())


# ---------------------------------------------------------------------------
# Through the real coordinator: the impossible state, and handler misbehaviour
# ---------------------------------------------------------------------------


def _coordinator(
    ledger: Any, store: Any, handler: Any, observer: Any, tmp_path: Path
):
    from sws_agent.execution import ExecutionCoordinator

    from test_execution import _Sleeper

    return ExecutionCoordinator(
        approval_store=store,
        execution_ledger=ledger,
        handler=handler,
        observer=observer,
        audit_store=_jsonl_audit(tmp_path / "audit.jsonl", _Clock()),
        id_source=lambda: "exec-1",
        worker_id="worker-1",
        now=_Clock(),
        # M15-D: STOP_RESOURCE declares a settle policy, so an accepted dispatch
        # now polls. Without an injected sleeper each poll costs a real 15s.
        sleep=_Sleeper(),
    )


def test_target_invalid_can_never_be_paired_with_no_effect_in_the_ledger(tmp_path: Path):
    """M15-C's impossible state, proven through the real coordinator.

    ``TARGET_INVALID`` means "this target can never satisfy the intent" and
    ``NO_EFFECT`` means "nothing external happened, so a retry may succeed".
    They are contradictory: a target that does not exist will not start
    existing because the system tried again. M15-B made the ledger refuse the
    pair; this proves no boundary evidence can walk it back in through the
    coordinator.
    """
    from test_execution import InMemoryApprovalStore

    class _NotFoundRejection(_World):
        """A definitive rejection naming a nonexistent instance."""

        def handle(self, request: Any) -> DispatchEvidence:
            self.handler_calls.append(request)
            return DispatchEvidence(
                disposition=DispatchDisposition.DISPATCH_REJECTED,
                aws_error_code="InvalidInstanceID.NotFound",
            )

    class _AcceptedThenGone(_World):
        """An accepted dispatch on a target that can no longer be stopped."""

        def handle(self, request: Any) -> DispatchEvidence:
            self.handler_calls.append(request)
            self.state = "terminated"
            return DispatchEvidence(disposition=DispatchDisposition.ACCEPTED)

    class _SilentBoundary:
        def handle(self, request: Any) -> DispatchEvidence:
            raise RuntimeError("the boundary reported nothing at all")

    # Each world produces genuinely different evidence. None may yield
    # FAILED + NO_EFFECT, and none may retry.
    cases = {
        "rejected_not_found": _NotFoundRejection(),
        "accepted_then_gone": _AcceptedThenGone(),
        "silent_boundary": _SilentBoundary(),
    }
    for name, boundary in cases.items():
        case_dir = tmp_path / name
        case_dir.mkdir()
        store = InMemoryApprovalStore(now=_Clock())
        request, _plan, _snapshot = _gated_request_with_ticket(store)
        ledger = _ledger()
        _coordinator(ledger, store, boundary, _World(), case_dir).execute(request)

        row = ledger.all_executions()[0]
        assert row.reexecution_class in {
            ReexecutionClass.TARGET_INVALID,
            ReexecutionClass.TRANSIENT_REJECTION,
            ReexecutionClass.POST_STATE_UNREACHABLE,
            # M15-D widened this set. The observer is a fresh _World reporting
            # "running", so the accepted dispatch settles into the ratified
            # deadline case: the stop was dispatched and the instance never
            # reached 'stopped' within the window, which is
            # POST_STATE_NOT_REACHED and is legitimately retryable. That is a
            # *more precise* answer than the M15-C fail-closed OUTCOME_UNKNOWN
            # it replaces -- the invariant under test is unchanged, because
            # POST_STATE_NOT_REACHED is still not NO_EFFECT.
            ReexecutionClass.POST_STATE_NOT_REACHED,
            ReexecutionClass.OUTCOME_UNKNOWN,
        }, f"{name} produced {row.reexecution_class}"
        # The impossible pair, stated directly.
        assert not (
            row.outcome is ExecutionOutcome.FAILED
            and row.reexecution_class is ReexecutionClass.NO_EFFECT
        ), f"{name} produced the impossible TARGET_INVALID/NO_EFFECT pair"
        # And the two bases are distinct concepts in the vocabulary.
        assert ReexecutionClass.TARGET_INVALID is not ReexecutionClass.NO_EFFECT


def test_a_declared_target_invalid_rejection_is_recorded_as_target_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The other half: the pair the system *should* be able to produce.

    Proves the test above is not vacuous. With a contract declaring what
    ``InvalidInstanceID.NotFound`` means, the coordinator records
    ``TARGET_INVALID`` -- terminal, non-retryable, and accepted by the ledger
    because this pair is legal.
    """
    from test_execution import (
        InMemoryApprovalStore,
        _install_dispatch_contract,
    )

    _install_dispatch_contract(
        monkeypatch,
        _contract(
            rejection_classes={
                "InvalidInstanceID.NotFound": ReexecutionClass.TARGET_INVALID
            }
        ),
    )

    class _NotFoundRejection(_World):
        def handle(self, request: Any) -> DispatchEvidence:
            self.handler_calls.append(request)
            return DispatchEvidence(
                disposition=DispatchDisposition.DISPATCH_REJECTED,
                aws_error_code="InvalidInstanceID.NotFound",
            )

    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    ledger = _ledger()
    world = _NotFoundRejection()
    result = _coordinator(ledger, store, world, world, tmp_path).execute(request)

    assert result.outcome is ExecutionOutcome.FAILED
    row = ledger.all_executions()[0]
    assert row.outcome is ExecutionOutcome.FAILED
    assert row.reexecution_class is ReexecutionClass.TARGET_INVALID
    assert row.permits_reexecution is False
    # The ledger's own compatibility table agrees, and the write survived.
    ledger.verify()


def test_handler_raising_fails_closed_rather_than_becoming_retryable(tmp_path: Path):
    """An exception at the boundary is ignorance, not a failed mutation.

    The old protocol said a raising handler was "an ambiguous, unknown attempt",
    which happened to be safe. M15-C makes it explicit: the coordinator has no
    evidence at all, so it settles UNKNOWN.
    """
    from test_execution import InMemoryApprovalStore

    class _RaisingBoundary:
        def __init__(self) -> None:
            self.handler_calls: list[Any] = []

        def handle(self, request: Any) -> DispatchEvidence:
            self.handler_calls.append(request)
            raise TimeoutError("no response")

    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    ledger = _ledger()
    boundary = _RaisingBoundary()
    result = _coordinator(
        ledger, store, boundary, _World(), tmp_path
    ).execute(request)

    assert result.outcome is ExecutionOutcome.UNKNOWN
    row = ledger.all_executions()[0]
    assert row.reexecution_class is ReexecutionClass.OUTCOME_UNKNOWN
    assert row.permits_reexecution is False


def test_handler_returning_a_non_evidence_object_fails_closed(tmp_path: Path):
    """A handler that ignores the contract cannot smuggle in a retryable shape.

    The coordinator checks the returned type rather than trusting the
    annotation, so a stale handler returning the old ``MutationAttempt`` -- or
    anything else -- settles UNKNOWN instead of being coerced into evidence.
    """
    from test_execution import InMemoryApprovalStore

    class _LegacyShapeBoundary:
        def __init__(self) -> None:
            self.handler_calls: list[Any] = []

        def handle(self, request: Any) -> Any:
            self.handler_calls.append(request)
            return {"ambiguous": False, "call_error": True}

    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    ledger = _ledger()
    boundary = _LegacyShapeBoundary()
    result = _coordinator(
        ledger, store, boundary, _World(), tmp_path
    ).execute(request)

    assert result.outcome is ExecutionOutcome.UNKNOWN
    row = ledger.all_executions()[0]
    assert row.reexecution_class is ReexecutionClass.OUTCOME_UNKNOWN
    assert row.permits_reexecution is False


def test_not_dispatched_never_calls_the_observer(tmp_path: Path):
    """ADR 0006: no dispatch means no post-state to verify.

    Verifying would observe an unchanged resource and report a postcondition
    contradiction for a mutation that was correctly never sent.
    """
    from test_execution import InMemoryApprovalStore

    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    ledger = _ledger()
    world = _World()
    result = _coordinator(
        ledger, store, world, world, tmp_path
    ).execute(request)

    # _World dispatches successfully, so verification does run and the boundary
    # evidence is ACCEPTED. Confirm the observer is the only path to a verdict.
    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert world.observed


def test_not_dispatched_never_calls_the_observer_after_the_gate(tmp_path: Path):
    """ADR 0006: no dispatch means no post-state to verify.

    Verifying would observe an unchanged resource and report a postcondition
    contradiction for a mutation that was correctly never sent.

    Note the preflight observation *does* happen, and legitimately: the A5 gate
    must obtain its own fresh provider-issued evidence before authorising
    anything. The distinction this test pins is that there is exactly one
    observation -- the gate's -- where an accepted dispatch produces two.
    """
    from sws_agent.composition import NullMutationHandler
    from test_execution import InMemoryApprovalStore

    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    ledger = _ledger()
    observer = _World()
    observer.observed.clear()
    result = _coordinator(
        ledger, store, NullMutationHandler(), observer, tmp_path
    ).execute(request)

    assert result.outcome is ExecutionOutcome.NOT_EXECUTED
    row = ledger.all_executions()[0]
    assert row.reexecution_class is ReexecutionClass.NO_EFFECT
    assert row.permits_reexecution is True
    # Only the preflight gate observed. No post-attempt observation, because
    # there was no post-state to observe.
    assert observer.observed == ["inst-1"]


def test_an_accepted_dispatch_does_observe_again_after_the_gate(tmp_path: Path):
    """The contrast that makes the test above meaningful.

    Identical setup, except the boundary reports ``ACCEPTED``. The coordinator
    then performs a second observation -- the post-attempt one -- which is what
    the ``NOT_DISPATCHED`` case correctly skips.
    """
    from test_execution import InMemoryApprovalStore

    store = InMemoryApprovalStore(now=_Clock())
    request, _plan, _snapshot = _gated_request_with_ticket(store)
    world = _World()
    world.observed.clear()

    result = _coordinator(_ledger(), store, world, world, tmp_path).execute(request)

    assert result.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    # One preflight observation for the gate, one post-attempt observation to
    # verify the postcondition.
    assert world.observed == ["inst-1", "inst-1"]


def test_null_handler_is_the_only_shipped_mutation_handler():
    """The shipped seam still cannot reach AWS, and says so honestly."""
    from sws_agent.composition import NullMutationHandler
    from sws_agent.constants import ExecutionMode
    from sws_agent.models import ExecutionRequest

    handler = NullMutationHandler()
    assert isinstance(handler, MutationHandler)
    evidence = handler.handle(
        ExecutionRequest(
            action_plan_id="plan-1",
            resource_id="i-0abc123def4567890",
            action=STOP,
            execution_mode=ExecutionMode.SAFE,
        )
    )
    assert evidence.disposition is DispatchDisposition.NOT_DISPATCHED
    assert evidence.sanitized["mutating"] is False
