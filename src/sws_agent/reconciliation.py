"""Reconcile the local dispatch witness against AWS-side CloudTrail evidence.

Two different claims are in play, and they are not the same claim:

- :mod:`sws_agent.mutation_evidence` observes what the *dispatching process*
  sent. It is precise, cheap, and entirely under the control of the code that
  made the mistake. A defect in that process -- or a defect in the observer --
  can produce a confident false pass.
- CloudTrail records what AWS *received and processed*. It is produced by a
  different system, with a different failure mode, and it is not reachable from
  the dispatching process.

The ruling is explicit about precedence:

    If those disagree, the AWS-side evidence wins for determining what actually
    happened, and the run should be treated as unsafe/ambiguous rather than
    trusting the local witness.

So this module is deliberately asymmetric. It has three outcomes and only one of
them is a pass:

``CONFIRMED``
    CloudTrail shows exactly one ``StopInstances`` event for the intended
    principal, target and time window, *and* the witness agrees.

``AWS_SIDE_DISAGREEMENT``
    AWS-side evidence contradicts the witness -- a different count, a different
    target, or a different principal. This is an incident. The CloudTrail record
    is taken as the fact; the witness is treated as the thing under suspicion.

``UNRESOLVED``
    CloudTrail is unavailable, errored, or cannot attribute the event
    unambiguously. This is explicitly *not* downgraded to "probably fine": an
    unverified run is an unverified run, and the ruling says so.

``NO_DISPATCH_OBSERVED``
    CloudTrail confirms nothing was dispatched and the witness agrees. A verified
    absence rather than an open question -- and still not a single-dispatch
    confirmation.

There is no code path here that returns a pass without CloudTrail evidence, and
no code path that lets a clean witness substitute for it.

``NO_DISPATCH_OBSERVED`` is a fourth outcome, kept separate from
``UNRESOLVED`` for a specific reason: when both sides agree that nothing was
dispatched, that is a *verified* absence, and reporting it as an open question
would be its own kind of dishonesty. It is still not ``CONFIRMED`` -- the
milestone's claim is that exactly one dispatch happened, and "nothing happened"
does not satisfy it.

It is also **not** a dispatch prerequisite. CloudTrail cannot confirm a dispatch
that has not happened, so requiring it before authorization would be circular.
This module is only ever called after dispatch; that separation is asserted by a
test rather than left to discipline.
"""

from __future__ import annotations

import dataclasses
import enum
from datetime import datetime
from typing import Sequence, TypeGuard

__all__ = [
    "CloudTrailStopEvent",
    "CloudTrailEvidence",
    "ReconciliationOutcome",
    "ReconciliationFinding",
    "ReconciliationReport",
    "ReconciliationIncident",
    "reconcile_dispatch",
]

STOP_INSTANCES_EVENT_NAME = "StopInstances"
EC2_EVENT_SOURCE = "ec2.amazonaws.com"


class ReconciliationOutcome(enum.Enum):
    """The three possible reconciliations. Two of them are failures."""

    #: Exactly one AWS-side event, matching the witness, for the intended target.
    CONFIRMED = "confirmed"

    #: AWS-side evidence contradicts the witness. Treated as an incident, with
    #: the CloudTrail record taken as the fact.
    AWS_SIDE_DISAGREEMENT = "aws_side_disagreement"

    #: CloudTrail is unavailable, errored, or ambiguous. Never a pass.
    UNRESOLVED = "unresolved"

    #: Both sides agree nothing was dispatched. Verified absence, not a
    #: confirmation of the single-dispatch claim.
    NO_DISPATCH_OBSERVED = "no_dispatch_observed"


class ReconciliationIncident(RuntimeError):
    """Raised when a caller demands a clean reconciliation that does not exist.

    Naming the exception separately from ``ReconciliationReport`` keeps the
    distinction between "the reconciliation ran" and "the reconciliation passed",
    which is the distinction this whole module exists to protect.
    """

    def __init__(self, report: ReconciliationReport) -> None:
        super().__init__(report.reason())
        self.report = report


@dataclasses.dataclass(frozen=True)
class CloudTrailStopEvent:
    """One ``StopInstances`` event as AWS recorded it.

    Fields mirror CloudTrail's own shape rather than a normalized form, because
    normalizing would discard exactly the things that distinguish a good match
    from a coincidence -- the raw principal, the raw time, the error code.
    """

    event_id: str
    event_time: datetime
    event_name: str
    event_source: str
    #: e.g. ``arn:aws:sts::123456789012:assumed-role/SwsStopRole/session``.
    principal_arn: str | None = None
    #: The STS session name, which is what identifies *this process's* session.
    session_name: str | None = None
    instance_ids: tuple[str, ...] = ()
    #: ``None`` when AWS processed the call; set when it was rejected.
    error_code: str | None = None
    read_only: bool = False

    @property
    def is_stop_instances(self) -> bool:
        return self.event_name == STOP_INSTANCES_EVENT_NAME

    @property
    def is_ec2(self) -> bool:
        return self.event_source == EC2_EVENT_SOURCE


@dataclasses.dataclass(frozen=True)
class CloudTrailEvidence:
    """The result of querying CloudTrail, including the ways that can fail.

    ``available`` is separate from ``events`` on purpose. "The query returned no
    events" and "the query never ran" are both an empty tuple, and conflating
    them is precisely the "probably fine" failure the ruling forbids.
    """

    available: bool
    events: tuple[CloudTrailStopEvent, ...] = ()
    #: Why the query failed, for the report. Never interpreted as benign.
    error: str | None = None
    #: The principal that ran the CloudTrail query.
    reader_principal_arn: str | None = None


@dataclasses.dataclass(frozen=True)
class ReconciliationFinding:
    check: str
    passed: bool
    detail: str
    evidence: str


@dataclasses.dataclass(frozen=True)
class ReconciliationReport:
    outcome: ReconciliationOutcome
    findings: tuple[ReconciliationFinding, ...]
    #: AWS-side count of in-window ``StopInstances`` events for the target.
    #: ``None`` when CloudTrail could not be consulted.
    aws_side_count: int | None = None

    @property
    def confirmed(self) -> bool:
        return self.outcome is ReconciliationOutcome.CONFIRMED

    @property
    def failures(self) -> tuple[ReconciliationFinding, ...]:
        return tuple(f for f in self.findings if not f.passed)

    @property
    def retry_permissible(self) -> bool:
        """Whether this evidence establishes that nothing was dispatched.

        True only for ``NO_DISPATCH_OBSERVED``, and exposed as code rather than
        left in prose because it is the one property with teeth: it is what
        licenses a second attempt, and every other outcome -- including
        ``UNRESOLVED`` and a confirmed absence built on an unattributable
        out-of-window event -- must refuse it.

        The ruling never relaxes into "probably fine". If CloudTrail could not
        tell us what happened, a retry would be a guess with an instance
        attached.
        """
        return self.outcome is ReconciliationOutcome.NO_DISPATCH_OBSERVED

    def reason(self) -> str:
        if self.confirmed:
            return "CloudTrail and the local witness agree on exactly one dispatch"
        return "; ".join(f"{f.check}: {f.detail}" for f in self.failures)

    def rows(self) -> tuple[tuple[str, str, str, str], ...]:
        return tuple(
            (f.check, "pass" if f.passed else "FAIL", f.detail, f.evidence)
            for f in self.findings
        )


def require_reconciliation(report: ReconciliationReport) -> None:
    """Raise unless CloudTrail and the witness agreed on exactly one dispatch."""
    if not report.confirmed:
        raise ReconciliationIncident(report)


# -- helpers ------------------------------------------------------------------


def _aware(value: datetime | None) -> TypeGuard[datetime]:
    """Whether ``value`` is timezone-aware.

    A ``TypeGuard`` rather than ``bool`` on purpose. As a plain predicate mypy
    cannot narrow through it, so every ``start.isoformat()`` after an
    ``if not _aware(start): return ...`` guard was an error -- and the obvious
    "fix" was the ``type: ignore`` that hid a real narrowing problem instead of
    expressing it.
    """
    return isinstance(value, datetime) and value.tzinfo is not None


def _in_window(event: CloudTrailStopEvent, start: datetime, end: datetime) -> bool:
    return start <= event.event_time <= end


def _describe(events: Sequence[CloudTrailStopEvent]) -> str:
    if not events:
        return "no events"
    parts = [
        f"{e.event_id}@{e.event_time.isoformat()}"
        f"[principal={e.principal_arn},targets={list(e.instance_ids)}"
        f",error={e.error_code}]"
        for e in events
    ]
    return " ".join(parts)


def _in_scope_events(
    evidence: CloudTrailEvidence,
    *,
    instance_id: str,
    start: datetime,
    end: datetime,
) -> tuple[CloudTrailStopEvent, ...]:
    """``StopInstances`` events in-window that touched ``instance_id``.

    Restricted to the target deliberately: the question is whether *this*
    instance was stopped, and unrelated activity must not create ambiguity. It
    is not restricted to the principal, because an event by a *different*
    principal on the same instance is a finding, not noise.
    """
    return tuple(
        event
        for event in evidence.events
        if event.is_stop_instances
        and event.is_ec2
        and not event.read_only
        and _in_window(event, start, end)
        and instance_id in event.instance_ids
    )


# -- the reconciliation -------------------------------------------------------


def reconcile_dispatch(
    *,
    expected_instance_id: str,
    expected_principal_arn: str,
    window_start: datetime,
    window_end: datetime,
    witness_attempts: int,
    witness_instance_ids: tuple[str, ...],
    cloudtrail: CloudTrailEvidence,
    session_name: str | None = None,
) -> ReconciliationReport:
    """Decide whether AWS confirms exactly one dispatch of the intended target.

    Ordering is deliberate and load-bearing. Availability is checked first, then
    attribution, then the count. Each stage can only move the outcome away from
    ``CONFIRMED``, so there is no ordering in which a late check rescues a run
    that an early one already failed -- and, more importantly, no ordering in
    which a clean witness short-circuits the CloudTrail requirement.
    """
    findings: list[ReconciliationFinding] = []

    findings.extend(
        _check_window(window_start, window_end, expected_instance_id)
    )

    if not _window_usable(window_start, window_end):
        return _unresolved(
            findings,
            "the correlation window is not usable, so no AWS-side evidence can be "
            "attributed to this run",
            aws_side_count=None,
        )

    findings.extend(_check_availability(cloudtrail, expected_principal_arn))
    if not cloudtrail.available:
        return _unresolved(
            findings,
            "CloudTrail could not be consulted, so the run cannot be verified; "
            "this is not downgraded to 'probably fine'",
            aws_side_count=None,
        )

    scoped = _in_scope_events(
        cloudtrail,
        instance_id=expected_instance_id,
        start=window_start,
        end=window_end,
    )
    matched = _by_principal(scoped, expected_principal_arn, session_name)
    others = [event for event in scoped if event not in matched]
    stray = _outside_window(
        cloudtrail, instance_id=expected_instance_id,
        start=window_start, end=window_end,
    )

    findings.extend(
        _check_attribution(
            scoped,
            matched,
            stray,
            expected_principal_arn=expected_principal_arn,
            expected_instance_id=expected_instance_id,
            session_name=session_name,
        )
    )
    findings.append(_check_target_scoping(scoped, expected_instance_id))
    findings.append(_check_aws_count(matched))
    findings.append(_check_witness_agreement(
        witness_attempts=witness_attempts,
        witness_instance_ids=witness_instance_ids,
        aws_side_count=len(matched),
    ))

    failed = [f for f in findings if not f.passed]
    if not failed:
        return ReconciliationReport(
            outcome=ReconciliationOutcome.CONFIRMED,
            findings=tuple(findings),
            aws_side_count=len(matched),
        )

    # AWS recorded nothing at all for this target while the local witness claims
    # it dispatched. That is a contradiction between the two evidence domains,
    # not an open question: AWS saw no call. Reported as a disagreement so it is
    # handled as an incident rather than filed as "nothing to see here".
    #
    # ``stray`` is excluded by this test and must be: a StopInstances event for
    # our target that lands outside the window means the window is wrong, which
    # makes "nothing was dispatched" unestablished rather than verified.
    if not scoped and not others and not stray and witness_attempts >= 1:
        return ReconciliationReport(
            outcome=ReconciliationOutcome.AWS_SIDE_DISAGREEMENT,
            findings=tuple(findings),
            aws_side_count=len(matched),
        )

    # Both sides agree nothing was dispatched. A verified absence, kept distinct
    # from UNRESOLVED so it is not mistaken for a pending question.
    #
    # This is also the only outcome that licenses a retry, so the ``not stray``
    # guard is load-bearing twice over. Without it, an unattributable out-of-window
    # event plus a witness that saw nothing would report a clean verified absence
    # -- and a retry would be authorised on the strength of an event we know
    # exists and could not place.
    if not scoped and not others and not stray and witness_attempts == 0:
        findings.append(
            ReconciliationFinding(
                "single_dispatch_claim",
                False,
                "CloudTrail and the witness agree that nothing was dispatched, so "
                "there is no single dispatch to confirm",
                f"aws_side_count=0 witness_attempts={witness_attempts}",
            )
        )
        return ReconciliationReport(
            outcome=ReconciliationOutcome.NO_DISPATCH_OBSERVED,
            findings=tuple(findings),
            aws_side_count=len(matched),
        )

    # Any other failure is an AWS-side disagreement or an unresolved attribution.
    # Both fail closed, and in neither case is the witness consulted as a
    # tie-breaker: the ruling is that AWS-side evidence decides.
    outcome = (
        ReconciliationOutcome.AWS_SIDE_DISAGREEMENT
        if matched or scoped
        else ReconciliationOutcome.UNRESOLVED
    )
    return ReconciliationReport(
        outcome=outcome,
        findings=tuple(findings),
        aws_side_count=len(matched),
    )


def _unresolved(
    findings: list[ReconciliationFinding],
    detail: str,
    *,
    aws_side_count: int | None,
) -> ReconciliationReport:
    findings.append(
        ReconciliationFinding(
            check="aws_side_evidence",
            passed=False,
            detail=detail,
            evidence="no usable CloudTrail evidence",
        )
    )
    return ReconciliationReport(
        outcome=ReconciliationOutcome.UNRESOLVED,
        findings=tuple(findings),
        aws_side_count=aws_side_count,
    )


def _window_usable(start: datetime | None, end: datetime | None) -> bool:
    return (
        _aware(start)
        and _aware(end)
        and isinstance(end, datetime)
        and start < end  # type: ignore[operator]
    )


def _check_window(
    start: datetime | None, end: datetime | None, instance_id: str
) -> list[ReconciliationFinding]:
    evidence = f"window_start={start!r} window_end={end!r} target={instance_id!r}"
    if not _aware(start):
        return [
            ReconciliationFinding(
                "correlation_window",
                False,
                "window_start is missing or timezone-naive, so events cannot be "
                "correlated in time",
                evidence,
            )
        ]
    if not _aware(end):
        return [
            ReconciliationFinding(
                "correlation_window",
                False,
                "window_end is missing or timezone-naive",
                evidence,
            )
        ]
    if start >= end:
        return [
            ReconciliationFinding(
                "correlation_window",
                False,
                f"window_start {start.isoformat()} is not before window_end "
                f"{end.isoformat()}",
                evidence,
            )
        ]
    return [
        ReconciliationFinding(
            "correlation_window",
            True,
            f"correlating {start.isoformat()} .. {end.isoformat()} for "
            f"{instance_id}",
            evidence,
        )
    ]


def _check_availability(
    cloudtrail: CloudTrailEvidence, expected_principal_arn: str
) -> list[ReconciliationFinding]:
    findings: list[ReconciliationFinding] = []
    if not cloudtrail.available:
        reason = cloudtrail.error or "the query reported failure without a reason"
        findings.append(
            ReconciliationFinding(
                "cloudtrail_available",
                False,
                f"CloudTrail evidence is unavailable: {reason}",
                f"available=False error={cloudtrail.error!r}",
            )
        )
        return findings
    findings.append(
        ReconciliationFinding(
            "cloudtrail_available",
            True,
            f"CloudTrail returned {len(cloudtrail.events)} event(s)",
            f"available=True events={len(cloudtrail.events)}",
        )
    )
    if cloudtrail.reader_principal_arn == expected_principal_arn:
        findings.append(
            ReconciliationFinding(
                "evidence_provenance",
                False,
                "the CloudTrail query ran as the mutating principal itself; the "
                "credential that can change things must not be the credential "
                "that inspects it",
                f"reader={cloudtrail.reader_principal_arn!r} "
                f"mutator={expected_principal_arn!r}",
            )
        )
    else:
        findings.append(
            ReconciliationFinding(
                "evidence_provenance",
                True,
                "CloudTrail was read by a separate read-only principal",
                f"reader={cloudtrail.reader_principal_arn!r}",
            )
        )
    return findings


def _by_principal(
    events: Sequence[CloudTrailStopEvent],
    expected_principal_arn: str,
    session_name: str | None,
) -> tuple[CloudTrailStopEvent, ...]:
    matched = []
    for event in events:
        if event.principal_arn != expected_principal_arn:
            continue
        if session_name is not None and event.session_name != session_name:
            continue
        matched.append(event)
    return tuple(matched)


def _outside_window(
    evidence: CloudTrailEvidence,
    *,
    instance_id: str,
    start: datetime,
    end: datetime,
) -> tuple[CloudTrailStopEvent, ...]:
    """Events for the same target and principal, but outside the declared window.

    Their presence means the window itself is wrong, so the run cannot be
    attributed unambiguously. Reporting them as findings is what turns a
    plausible-looking confirmation into an honest "unresolved".
    """
    return tuple(
        event
        for event in evidence.events
        if event.is_stop_instances
        and event.is_ec2
        and not event.read_only
        and instance_id in event.instance_ids
        and not _in_window(event, start, end)
    )


def _check_target_scoping(
    scoped: Sequence[CloudTrailStopEvent],
    expected_instance_id: str,
) -> ReconciliationFinding:
    """Every in-window event must have requested **exactly** the pinned target.

    Scoping an event by ``expected in event.instance_ids`` is necessary but not
    sufficient, and the gap is a live one: ``StopInstances`` accepts up to a
    thousand instance ids in a single call, so one event can carry our target
    *and* others. Counting that as one clean dispatch would confirm the
    single-dispatch claim while a second instance was stopped with the same
    request -- which the runbook treats as a program-level target-pinning failure.

    So the target set must equal the pinned id, not merely contain it. An event
    naming our target plus another is a failure, not a confirmation with a
    footnote.
    """
    multi = [
        event for event in scoped
        if set(event.instance_ids) != {expected_instance_id}
    ]
    evidence = (
        f"expected_targets=['{expected_instance_id}'] multi_target_events="
        f"{_describe(multi)}"
    )
    if multi:
        return ReconciliationFinding(
            "target_attribution",
            False,
            f"{len(multi)} in-window event(s) requested targets other than "
            f"{expected_instance_id}; a single call must name exactly the pinned "
            "instance, so the request was not the one intended",
            evidence,
        )
    return ReconciliationFinding(
        "target_attribution",
        True,
        f"every in-window event named exactly {expected_instance_id} and nothing "
        "else",
        evidence,
    )


def _check_attribution(
    scoped: Sequence[CloudTrailStopEvent],
    matched: Sequence[CloudTrailStopEvent],
    stray: Sequence[CloudTrailStopEvent],
    *,
    expected_principal_arn: str,
    expected_instance_id: str,
    session_name: str | None,
) -> list[ReconciliationFinding]:
    findings: list[ReconciliationFinding] = []
    evidence = (
        f"in_window_for_target={len(scoped)} matched_principal={len(matched)} "
        f"outside_window={len(stray)} expected_principal={expected_principal_arn!r} "
        f"expected_session={session_name!r} events={_describe(scoped)}"
    )

    others = [e for e in scoped if e not in matched]
    if others:
        principals = sorted({str(e.principal_arn) for e in others})
        sessions = sorted({str(e.session_name) for e in others})
        findings.append(
            ReconciliationFinding(
                "principal_attribution",
                False,
                f"{len(others)} in-window event(s) for {expected_instance_id} came "
                f"from a different principal or session: principals={principals} "
                f"sessions={sessions}",
                evidence,
            )
        )
    else:
        findings.append(
            ReconciliationFinding(
                "principal_attribution",
                True,
                "every in-window event for the target came from the intended "
                "principal and session",
                evidence,
            )
        )

    if stray:
        findings.append(
            ReconciliationFinding(
                "window_attribution",
                False,
                f"{len(stray)} event(s) for the same target fall outside the "
                "declared window, so the window is wrong and this run cannot be "
                "attributed unambiguously",
                f"{evidence} outside_window_events={_describe(stray)}",
            )
        )
    else:
        findings.append(
            ReconciliationFinding(
                "window_attribution",
                True,
                "no events for the target fall outside the declared window",
                evidence,
            )
        )
    return findings


def _check_aws_count(matched: Sequence[CloudTrailStopEvent]) -> ReconciliationFinding:
    count = len(matched)
    evidence = f"aws_side_count={count} events={_describe(matched)}"
    if count == 1:
        return ReconciliationFinding(
            "aws_side_count",
            True,
            "AWS recorded exactly one StopInstances event",
            evidence,
        )
    if count == 0:
        return ReconciliationFinding(
            "aws_side_count",
            False,
            "AWS recorded no StopInstances event for this target and principal, "
            "so the mutation was not processed as AWS saw it",
            evidence,
        )
    return ReconciliationFinding(
        "aws_side_count",
        False,
        f"AWS recorded {count} StopInstances events; the mutation was processed "
        "more than once regardless of what the local process observed",
        evidence,
    )


def _check_witness_agreement(
    *,
    witness_attempts: int,
    witness_instance_ids: tuple[str, ...],
    aws_side_count: int,
) -> ReconciliationFinding:
    """Cross-check, in that order: the witness must not contradict AWS.

    The witness is never allowed to *establish* the count -- ``aws_side_count``
    is the fact and this compares against it. When they disagree the finding
    fails, which is what routes the run to an incident.
    """
    evidence = (
        f"witness_attempts={witness_attempts} "
        f"witness_targets={list(witness_instance_ids)} aws_side_count={aws_side_count}"
    )
    problems: list[str] = []
    if witness_attempts != 1:
        problems.append(
            f"the local witness observed {witness_attempts} attempt(s)"
        )
    if aws_side_count != 1:
        problems.append(f"AWS recorded {aws_side_count} event(s)")
    if len(witness_instance_ids) != 1:
        problems.append(
            f"the local witness saw {len(witness_instance_ids)} target id(s)"
        )
    if problems:
        return ReconciliationFinding(
            "witness_vs_aws",
            False,
            "the local witness and AWS-side evidence do not agree; "
            + "; ".join(problems)
            + ". The AWS-side record is taken as the fact",
            evidence,
        )
    return ReconciliationFinding(
        "witness_vs_aws",
        True,
        "the local witness and AWS-side evidence agree on one attempt for one "
        "target",
        evidence,
    )