"""Tests for witness <-> CloudTrail reconciliation.

The property under test is a precedence rule, not a count:

    CloudTrail decides what actually happened. A clean local witness can never
    substitute for it, and a contradiction between them is an incident rather
    than something to resolve in the witness's favour.

Three things are falsified explicitly, because each is the way this module
could quietly become useless:

1. **AWS wins.** A witness reporting one clean attempt against two AWS-side
   events must fail, and must report the AWS count as the fact.
2. **Unavailability is not benign.** An unavailable CloudTrail must be
   ``UNRESOLVED`` even when the witness is perfect.
3. **No circularity.** The reconciler must not be reachable from the
   pre-dispatch authorization path, because CloudTrail cannot confirm a dispatch
   that has not happened.

No AWS access: CloudTrail evidence is constructed as data, and the witness side
uses real botocore with the transport stubbed.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import sws_agent.reconciliation as reconciliation_module
from sws_agent.reconciliation import (
    CloudTrailEvidence,
    CloudTrailStopEvent,
    ReconciliationIncident,
    ReconciliationOutcome,
    reconcile_dispatch,
    require_reconciliation,
)

INSTANCE_ID = "i-0abcdef1234567890"
OTHER_INSTANCE_ID = "i-0fffffffffffffff0"
MUTATOR_ARN = "arn:aws:sts::123456789012:assumed-role/SwsStopRole/sws-first-stop"
FORENSIC_ARN = "arn:aws:sts::123456789012:assumed-role/SwsForensics/sws-audit"
SESSION = "sws-first-stop"

START = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(seconds=30)


# -- fixtures -----------------------------------------------------------------


def _event(
    *,
    event_id: str = "evt-1",
    offset_seconds: float = 5.0,
    instance_ids: tuple[str, ...] = (INSTANCE_ID,),
    principal_arn: str | None = MUTATOR_ARN,
    session_name: str | None = SESSION,
    error_code: str | None = None,
    event_name: str = "StopInstances",
    event_source: str = "ec2.amazonaws.com",
    read_only: bool = False,
) -> CloudTrailStopEvent:
    return CloudTrailStopEvent(
        event_id=event_id,
        event_time=START + timedelta(seconds=offset_seconds),
        event_name=event_name,
        event_source=event_source,
        principal_arn=principal_arn,
        session_name=session_name,
        instance_ids=instance_ids,
        error_code=error_code,
        read_only=read_only,
    )


def _evidence(
    *events: CloudTrailStopEvent,
    available: bool = True,
    error: str | None = None,
    reader: str | None = FORENSIC_ARN,
) -> CloudTrailEvidence:
    return CloudTrailEvidence(
        available=available,
        events=tuple(events),
        error=error,
        reader_principal_arn=reader,
    )


def _reconcile(
    *,
    cloudtrail: CloudTrailEvidence,
    witness_attempts: int = 1,
    witness_instance_ids: tuple[str, ...] = (INSTANCE_ID,),
    instance_id: str = INSTANCE_ID,
    principal: str = MUTATOR_ARN,
    session: str | None = SESSION,
    window_start: datetime | None = START,
    window_end: datetime | None = END,
) -> Any:
    return reconcile_dispatch(
        expected_instance_id=instance_id,
        expected_principal_arn=principal,
        window_start=window_start,
        window_end=window_end,
        witness_attempts=witness_attempts,
        witness_instance_ids=witness_instance_ids,
        cloudtrail=cloudtrail,
        session_name=session,
    )


# -- the confirmation path ----------------------------------------------------


def test_one_matching_event_and_a_clean_witness_confirms():
    report = _reconcile(cloudtrail=_evidence(_event()))
    assert report.outcome is ReconciliationOutcome.CONFIRMED, report.reason()
    assert report.aws_side_count == 1
    require_reconciliation(report)


def test_a_confirmed_report_says_so_in_its_reason():
    report = _reconcile(cloudtrail=_evidence(_event()))
    assert "CloudTrail" in report.reason()


# -- rule 1: the AWS side wins ------------------------------------------------


def test_a_clean_witness_cannot_outvote_two_aws_events():
    """The central precedence rule.

    The witness reports one attempt and one target. AWS recorded two. If the
    module trusted the witness, this would confirm -- and the mutation would have
    been processed twice.
    """
    report = _reconcile(
        cloudtrail=_evidence(_event(event_id="evt-1"), _event(event_id="evt-2")),
        witness_attempts=1,
    )
    assert not report.confirmed
    assert report.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT
    assert report.aws_side_count == 2, "the AWS count is the fact, not the witness's"


def test_the_disagreement_finding_names_the_aws_count_as_the_fact():
    report = _reconcile(
        cloudtrail=_evidence(_event(), _event(event_id="evt-2")), witness_attempts=1
    )
    finding = next(f for f in report.failures if f.check == "witness_vs_aws")
    assert "AWS recorded 2 event(s)" in finding.detail
    assert "taken as the fact" in finding.detail


def test_two_aws_events_fail_even_if_the_witness_also_saw_two():
    """Agreement does not rescue a double dispatch.

    Both sides agreeing that two dispatches happened is not a pass; it is a
    confirmed violation. Reconciliation confirms *one* dispatch or nothing.
    """
    report = _reconcile(
        cloudtrail=_evidence(_event(), _event(event_id="evt-2")),
        witness_attempts=2,
    )
    assert not report.confirmed
    count = next(f for f in report.failures if f.check == "aws_side_count")
    assert "processed more than once" in count.detail


def test_a_clean_witness_with_zero_aws_events_is_not_a_pass():
    """The witness says it sent one; AWS says it processed none.

    This is the case where "probably fine" is most tempting and most wrong --
    for instance if the call was refused at the edge and the process believed it
    had sent something.
    """
    report = _reconcile(cloudtrail=_evidence(), witness_attempts=1)
    assert not report.confirmed
    count = next(f for f in report.failures if f.check == "aws_side_count")
    assert "not processed as AWS saw it" in count.detail


def test_the_outcome_enum_has_exactly_the_four_documented_members():
    """A new outcome would be a new pass/fail decision, so the set is pinned."""
    assert {m.name for m in ReconciliationOutcome} == {
        "CONFIRMED",
        "AWS_SIDE_DISAGREEMENT",
        "UNRESOLVED",
        "NO_DISPATCH_OBSERVED",
    }


def test_a_witness_that_saw_a_retry_fails_even_with_one_aws_event():
    """Two local attempts, one AWS event: a real violation of no-second-dispatch.

    The second attempt may not have been processed, but it was dispatched, and
    the invariant is about dispatch.
    """
    report = _reconcile(cloudtrail=_evidence(_event()), witness_attempts=2)
    assert not report.confirmed
    witness_check = next(f for f in report.failures if f.check == "witness_vs_aws")
    assert "observed 2 attempt(s)" in witness_check.detail


# -- rule 2: unavailability is never benign -----------------------------------


def test_an_unavailable_cloudrail_is_unresolved_not_probably_fine():
    report = _reconcile(
        cloudtrail=_evidence(available=False, error="AccessDenied on cloudtrail:LookupEvents")
    )
    assert report.outcome is ReconciliationOutcome.UNRESOLVED
    assert not report.confirmed
    assert report.aws_side_count is None


def test_unavailability_fails_even_when_the_witness_is_perfect():
    """Two local observations agreeing cannot substitute for AWS evidence."""
    report = _reconcile(
        cloudtrail=_evidence(available=False, error="throttled"),
        witness_attempts=1,
        witness_instance_ids=(INSTANCE_ID,),
    )
    assert not report.confirmed
    finding = next(f for f in report.failures if f.check == "cloudtrail_available")
    assert "throttled" in finding.detail


def test_requiring_reconciliation_raises_when_cloudtrail_is_missing():
    report = _reconcile(cloudtrail=_evidence(available=False, error="timeout"))
    with pytest.raises(ReconciliationIncident) as caught:
        require_reconciliation(report)
    assert caught.value.report is report
    assert "not downgraded" in str(caught.value) or "unavailable" in str(caught.value)


def test_an_unavailable_query_with_a_vague_error_is_still_unresolved():
    """A failure with no reason must not be treated as an empty result."""
    report = _reconcile(cloudtrail=_evidence(available=False, error=None))
    assert report.outcome is ReconciliationOutcome.UNRESOLVED
    finding = next(f for f in report.failures if f.check == "cloudtrail_available")
    assert "without a reason" in finding.detail


def test_unavailable_and_empty_are_different_things():
    """The distinction the ruling depends on, as a test.

    Both present an empty event tuple. If they were the same, a failed query
    would silently become "no events", and "no events" would then have to be read
    as "probably fine" for the module to be usable at all.
    """
    failed = _reconcile(cloudtrail=_evidence(available=False, error="boom"))
    empty = _reconcile(cloudtrail=_evidence())
    assert failed.aws_side_count is None
    assert empty.aws_side_count == 0
    assert failed.outcome is ReconciliationOutcome.UNRESOLVED
    assert empty.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT


def test_a_clean_witness_with_zero_aws_events_is_an_incident_not_a_question():
    """Zero events against a claimed dispatch is a contradiction, not ambiguity.

    CloudTrail answered unambiguously: no such call arrived. Reporting that as
    an open question would let a genuine contradiction hide behind "unresolved".
    """
    report = _reconcile(cloudtrail=_evidence(), witness_attempts=1)
    assert report.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT
    assert not report.confirmed


def test_both_sides_agreeing_on_nothing_is_a_verified_absence():
    """Distinct from UNRESOLVED, and still not a single-dispatch confirmation.

    When the witness also saw no attempt, the absence is corroborated. Filing
    that as an open question would misrepresent a settled fact as an open one.
    """
    report = _reconcile(cloudtrail=_evidence(), witness_attempts=0, witness_instance_ids=())
    assert report.outcome is ReconciliationOutcome.NO_DISPATCH_OBSERVED
    assert not report.confirmed, "nothing dispatched is not one dispatch confirmed"
    finding = next(f for f in report.failures if f.check == "single_dispatch_claim")
    assert "no single dispatch to confirm" in finding.detail


def test_requiring_reconciliation_raises_on_a_verified_absence():
    report = _reconcile(cloudtrail=_evidence(), witness_attempts=0, witness_instance_ids=())
    with pytest.raises(ReconciliationIncident):
        require_reconciliation(report)


# -- attribution: principal, session, window ---------------------------------


def test_an_event_from_another_principal_is_not_attributed_to_this_run():
    report = _reconcile(
        cloudtrail=_evidence(_event(principal_arn="arn:aws:sts::123456789012:assumed-role/Other/x"))
    )
    assert not report.confirmed
    finding = next(f for f in report.failures if f.check == "principal_attribution")
    assert "different principal" in finding.detail


def test_an_event_from_another_session_is_not_attributed_to_this_run():
    report = _reconcile(cloudtrail=_evidence(_event(session_name="some-other-session")))
    assert not report.confirmed
    finding = next(f for f in report.failures if f.check == "principal_attribution")
    assert "different principal or session" in finding.detail


def test_a_different_principal_stopping_the_same_target_is_an_incident():
    """Someone else stopping it is not noise; it is a finding about attribution."""
    report = _reconcile(
        cloudtrail=_evidence(
            _event(),
            _event(event_id="evt-2", principal_arn="arn:aws:sts::123456789012:user/someone"),
        )
    )
    assert report.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT
    finding = next(f for f in report.failures if f.check == "principal_attribution")
    assert "from a different principal" in finding.detail


def test_an_event_outside_the_window_makes_the_run_unattributable():
    """A single in-window match is not enough if the window itself is wrong."""
    report = _reconcile(
        cloudtrail=_evidence(_event(), _event(event_id="evt-late", offset_seconds=600.0))
    )
    assert not report.confirmed
    finding = next(f for f in report.failures if f.check == "window_attribution")
    assert "outside the declared window" in finding.detail


def test_an_unrelated_instance_does_not_create_ambiguity():
    """Only the target's own history can be ambiguous."""
    report = _reconcile(
        cloudtrail=_evidence(
            _event(),
            _event(event_id="evt-other", instance_ids=(OTHER_INSTANCE_ID,), offset_seconds=900.0),
        )
    )
    assert report.outcome is ReconciliationOutcome.CONFIRMED, report.reason()


def test_a_naive_timestamp_window_is_refused():
    """Timezone-naive windows cannot be correlated; they must not be assumed UTC."""
    report = _reconcile(
        cloudtrail=_evidence(_event()),
        window_start=datetime(2026, 10, 4, 12, 0, 0),
    )
    assert not report.confirmed
    finding = next(f for f in report.failures if f.check == "correlation_window")
    assert "timezone-naive" in finding.detail


def test_an_inverted_window_is_refused():
    report = _reconcile(
        cloudtrail=_evidence(_event()),
        window_start=END,
        window_end=START,
    )
    assert not report.confirmed
    finding = next(f for f in report.failures if f.check == "correlation_window")
    assert "not before" in finding.detail


# -- evidence provenance ------------------------------------------------------


def test_the_mutating_principal_may_not_read_its_own_evidence():
    """The credential that can change things must not be able to inspect itself.

    A witness plus a self-read CloudTrail would be one actor's account of itself,
    which is what the independent cross-check exists to avoid.
    """
    report = _reconcile(cloudtrail=_evidence(_event(), reader=MUTATOR_ARN))
    assert not report.confirmed
    finding = next(f for f in report.failures if f.check == "evidence_provenance")
    assert "must not be the credential" in finding.detail


def test_a_separate_read_only_principal_is_accepted():
    report = _reconcile(cloudtrail=_evidence(_event(), reader=FORENSIC_ARN))
    assert report.outcome is ReconciliationOutcome.CONFIRMED, report.reason()


# -- noise that must not be misread -------------------------------------------


def test_unrelated_operations_are_not_counted_as_dispatches():
    report = _reconcile(
        cloudtrail=_evidence(
            _event(),
            _event(event_id="evt-describe", event_name="DescribeInstances", read_only=True),
        )
    )
    assert report.outcome is ReconciliationOutcome.CONFIRMED, report.reason()


def test_a_non_ec2_stopinstances_event_is_not_counted():
    report = _reconcile(
        cloudtrail=_evidence(
            _event(), _event(event_id="evt-fake", event_source="not-ec2.amazonaws.com")
        )
    )
    assert report.outcome is ReconciliationOutcome.CONFIRMED, report.reason()


def test_a_rejected_call_still_counts_as_a_dispatch():
    """An errorCode means AWS did not apply it, but it was still one API event.

    Success versus failure is the settlement's question (post-state evidence);
    reconciliation only answers "how many dispatches did AWS process".
    """
    report = _reconcile(cloudtrail=_evidence(_event(error_code="UnauthorizedOperation")))
    assert report.outcome is ReconciliationOutcome.CONFIRMED, report.reason()
    assert report.aws_side_count == 1
    finding = next(f for f in report.findings if f.check == "aws_side_count")
    assert "UnauthorizedOperation" in finding.evidence


def test_two_rejected_calls_are_two_dispatches():
    report = _reconcile(
        cloudtrail=_evidence(
            _event(error_code="UnauthorizedOperation"),
            _event(event_id="evt-2", offset_seconds=6.0, error_code="UnauthorizedOperation"),
        )
    )
    assert not report.confirmed
    assert report.aws_side_count == 2


def test_a_witness_with_multiple_targets_is_not_a_pass():
    """A request naming more than one instance is a blast-radius failure."""
    report = _reconcile(
        cloudtrail=_evidence(_event()),
        witness_instance_ids=(INSTANCE_ID, OTHER_INSTANCE_ID),
    )
    assert not report.confirmed
    finding = next(f for f in report.failures if f.check == "witness_vs_aws")
    assert "2 target id(s)" in finding.detail


# -- against the real AWS event shape -----------------------------------------


def test_reconciliation_works_on_an_actual_lookupevents_record():
    """Field names come from the API reference, not from how we wish they were.

    The other tests build ``CloudTrailStopEvent`` by hand, which means they would
    keep passing if our idea of the record shape drifted from AWS's. This one
    starts from a realistic ``LookupEvents`` response and reads the target out of
    it, so the shape is pinned against something external.

    Note the two places the target appears: ``Resources[].ResourceId`` and
    ``RequestParameters.instanceIdsSet.items[].instanceId``. They agree in a
    healthy record, and a disagreement is itself evidence of something odd -- but
    ``RequestParameters`` is the authoritative statement of what was *requested*,
    so that is what is read.
    """
    raw = {
        "EventId": "11111111-2222-3333-4444-555555555555",
        "EventName": "StopInstances",
        "EventSource": "ec2.amazonaws.com",
        "EventTime": START + timedelta(seconds=5),
        "Username": "AROAEXAMPLE:sws-first-stop",
        "Resources": [{"ResourceType": "Instance", "ResourceId": INSTANCE_ID}],
        "RequestParameters": {
            "instanceIdsSet": {"items": [{"instanceId": INSTANCE_ID}]}
        },
        "ResponseElements": {
            "instancesSet": {
                "items": [
                    {
                        "instanceId": INSTANCE_ID,
                        "currentState": {"name": "stopping"},
                        "previousState": {"name": "running"},
                    }
                ]
            }
        },
        "eventType": "AwsApiCall",
        "readOnly": False,
        "recipientAccountId": "123456789012",
    }
    # Both sources of the target agree; assert that before relying on it.
    assert raw["Resources"][0]["ResourceId"] == INSTANCE_ID
    requested = raw["RequestParameters"]["instanceIdsSet"]["items"]
    assert [i["instanceId"] for i in requested] == [INSTANCE_ID]

    event = CloudTrailStopEvent(
        event_id=raw["EventId"],
        event_time=raw["EventTime"],
        event_name=raw["EventName"],
        event_source=raw["EventSource"],
        principal_arn=MUTATOR_ARN,
        session_name=raw["Username"].split(":", 1)[1],
        instance_ids=tuple(i["instanceId"] for i in requested),
        read_only=raw["readOnly"],
    )
    report = _reconcile(
        cloudtrail=_evidence(event),
        session=raw["Username"].split(":", 1)[1],
    )
    assert report.outcome is ReconciliationOutcome.CONFIRMED, report.reason()


def test_the_session_name_is_recovered_from_the_username_field():
    """``Username`` is ``ROLE_ID:session``, which is where the session lives.

    Reading it this way is what lets the reconciliation separate two concurrent
    sessions of the same role.
    """
    username = "AROAEXAMPLE:sws-first-stop"
    role_id, _, session = username.partition(":")
    assert role_id == "AROAEXAMPLE"
    assert session == "sws-first-stop"
    report = _reconcile(
        cloudtrail=_evidence(_event(session_name="sws-other-session")),
        session=session,
    )
    assert not report.confirmed, "a different session must not confirm this run"


# -- rule 3: no circularity ---------------------------------------------------


def test_the_reconciler_is_not_imported_by_the_preflight_authorization_path():
    """CloudTrail cannot confirm a dispatch that has not happened.

    If the pre-dispatch gate required reconciliation, the run could never start.
    The dependency is asserted structurally so a future refactor cannot quietly
    reintroduce the cycle.
    """
    import sws_agent.preflight as preflight_module

    source = Path(preflight_module.__file__).read_text(encoding="utf-8")
    assert "reconciliation" not in source
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("reconcil" in name for name in imported)


def test_reconcile_dispatch_requires_witness_observations_to_exist_first():
    """Witness attempts are a required argument, not an optional extra.

    A default of zero would let a caller reconcile a run that never armed a
    witness, which is exactly the shape of "verified from nothing".
    """
    signature = inspect.signature(reconcile_dispatch)
    assert "witness_attempts" in signature.parameters
    assert "cloudtrail" in signature.parameters
    for name in ("witness_attempts", "witness_instance_ids", "cloudtrail"):
        assert (
            signature.parameters[name].default is inspect.Parameter.empty
        ), f"{name} must be required, not defaulted"
        # Keyword-only: a caller cannot pass evidence positionally by accident.
        assert (
            signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        ), f"{name} must be keyword-only"
    assert signature.parameters["witness_attempts"].annotation == "int"


def test_the_reconciliation_module_performs_no_network_access():
    """It consumes CloudTrail evidence as data; it never fetches it."""
    source = Path(reconciliation_module.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]
    for forbidden in ("boto3", "botocore", "import requests", "urlopen", "LookupEvents"):
        assert forbidden not in body, f"reconciliation must not contain {forbidden!r}"


def test_the_reconciliation_module_never_dispatches():
    """AST-based, because every text-based version of this check was wrong.

    A bare substring matches ``is_stop_instances``; adding a call parenthesis
    still matches ``is_stop_instances(self)``. Both were tried and both failed on
    a property this module legitimately defines. Only looking at actual ``Call``
    nodes distinguishes "defines a predicate" from "calls the SDK".
    """
    source = Path(reconciliation_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                called.add(func.attr)
            elif isinstance(func, ast.Name):
                called.add(func.id)
    for forbidden in ("stop_instances", "describe_instances"):
        assert forbidden not in called, f"reconciliation must not call {forbidden!r}"


def test_the_property_that_collides_with_the_call_check_is_really_a_property():
    """So the check above is not passing merely because the name is absent."""
    assert isinstance(
        inspect.getattr_static(CloudTrailStopEvent, "is_stop_instances"),
        property,
    )
    assert CloudTrailStopEvent(
        event_id="e",
        event_time=START,
        event_name="StopInstances",
        event_source="ec2.amazonaws.com",
    ).is_stop_instances is True


def test_the_reconciler_exposes_no_aws_call_helper():
    """Nothing here can go and fetch the evidence it reconciles.

    Reconciliation consumes what a separate step already retrieved, so that the
    retrieval and the judgement stay separable -- a module that both queried and
    judged could quietly retry, widen the window, or drop events without that
    showing up in the verdict.
    """
    assert not hasattr(reconciliation_module, "fetch_events")
    assert not hasattr(reconciliation_module, "lookup_events")
    assert not hasattr(reconciliation_module, "query_cloudtrail")


# -- evidence is carried ------------------------------------------------------


def test_every_finding_records_detail_and_evidence():
    report = _reconcile(cloudtrail=_evidence(_event(), _event(event_id="evt-2")))
    assert report.findings
    for finding in report.findings:
        assert finding.detail.strip(), finding.check
        assert finding.evidence.strip(), finding.check


def test_the_report_rows_name_every_check():
    report = _reconcile(cloudtrail=_evidence(_event()))
    checks = {row[0] for row in report.rows()}
    assert {
        "correlation_window",
        "cloudtrail_available",
        "evidence_provenance",
        "principal_attribution",
        "window_attribution",
        "aws_side_count",
        "witness_vs_aws",
    } <= checks


def test_only_confirmed_is_a_pass_every_other_outcome_fails_closed():
    """One pass, three non-passes -- asserted so a new outcome cannot be a silent pass."""
    observed = {
        ReconciliationOutcome.CONFIRMED: _reconcile(cloudtrail=_evidence(_event())),
        ReconciliationOutcome.AWS_SIDE_DISAGREEMENT: _reconcile(
            cloudtrail=_evidence(_event(), _event(event_id="evt-2"))
        ),
        ReconciliationOutcome.UNRESOLVED: _reconcile(
            cloudtrail=_evidence(available=False, error="x")
        ),
        ReconciliationOutcome.NO_DISPATCH_OBSERVED: _reconcile(
            cloudtrail=_evidence(), witness_attempts=0, witness_instance_ids=()
        ),
    }
    assert set(observed) == set(ReconciliationOutcome)
    for outcome, report in observed.items():
        assert report.outcome is outcome
        assert report.confirmed is (outcome is ReconciliationOutcome.CONFIRMED)
        assert bool(report.failures) is not report.confirmed
        # A retry is licensed by exactly one outcome. Exposed as a property so the
        # rule is mechanical; leaving it in prose is how a stray-event bug below
        # managed to authorise a re-dispatch.
        assert report.retry_permissible is (
            outcome is ReconciliationOutcome.NO_DISPATCH_OBSERVED
        )
        if not report.confirmed:
            with pytest.raises(ReconciliationIncident):
                require_reconciliation(report)


# -- falsifications for two defects found while reviewing ----------------------


def test_a_multi_target_event_does_not_count_as_one_clean_dispatch() -> None:
    """``StopInstances`` takes up to a thousand ids per call.

    One event naming our instance *and* another is a single request that stopped
    two machines. Scoping by ``expected in targets`` accepted it, the count came
    out at 1, and the run confirmed -- while a second instance went down with the
    same call. The runbook treats a wrong target as a program-level failure, so
    the target set must equal the pinned id rather than contain it.
    """
    event = _event(instance_ids=(INSTANCE_ID, OTHER_INSTANCE_ID))
    report = _reconcile(cloudtrail=_evidence(event))
    assert not report.confirmed
    finding = next(f for f in report.findings if f.check == "target_attribution")
    assert not finding.passed
    assert OTHER_INSTANCE_ID in finding.evidence
    assert report.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT
    assert not report.retry_permissible
    with pytest.raises(ReconciliationIncident):
        require_reconciliation(report)


def test_a_multi_target_event_is_a_failure_even_with_a_clean_witness() -> None:
    """One witness attempt, one AWS event, correct principal and session.

    Everything else agrees, which is exactly the shape that would confirm under a
    containment check.
    """
    report = _reconcile(
        cloudtrail=_evidence(
            _event(instance_ids=(INSTANCE_ID, OTHER_INSTANCE_ID))
        ),
        witness_attempts=1,
        witness_instance_ids=(INSTANCE_ID,),
    )
    assert not report.confirmed
    assert any(f.check == "target_attribution" and not f.passed for f in report.findings)


def test_a_stray_event_does_not_authorise_a_retry() -> None:
    """The defect this milestone nearly shipped.

    A ``StopInstances`` event for our target outside the declared window means
    the window is wrong. With a witness that saw nothing, the outcome branches
    tested only ``scoped`` and ``others``, so the run reported
    ``NO_DISPATCH_OBSERVED`` -- a *verified* absence, and the one outcome that
    licenses a retry. So an event known to exist, that could not be placed in
    time, would have authorised a second dispatch against an instance that may
    already have been stopped.

    That is the exact failure the ruling names, so it is pinned here.
    """
    stray = _event(event_id="evt-stray", offset_seconds=600.0)
    assert stray.event_time > END  # outside the declared window, as intended
    report = _reconcile(
        cloudtrail=_evidence(stray),
        witness_attempts=0,
        witness_instance_ids=(),
    )
    assert report.outcome is not ReconciliationOutcome.NO_DISPATCH_OBSERVED
    assert not report.retry_permissible
    assert report.outcome is ReconciliationOutcome.UNRESOLVED
    assert any(f.check == "window_attribution" and not f.passed for f in report.findings)
    with pytest.raises(ReconciliationIncident):
        require_reconciliation(report)


def test_a_stray_event_with_a_witness_attempt_is_still_not_a_dispatch() -> None:
    """Same stray, but the witness claims one attempt.

    Now AWS-side evidence exists and is unattributable in time, so this is a
    disagreement rather than a clean absence -- and still not retryable.
    """
    report = _reconcile(
        cloudtrail=_evidence(_event(event_id="evt-stray", offset_seconds=600.0)),
        witness_attempts=1,
    )
    assert not report.retry_permissible
    assert not report.confirmed