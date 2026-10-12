"""Hermetic tests for the M16 read-only forensic CloudTrail reader.

No AWS, no network: the ``cloudtrail:LookupEvents`` paginator is a local fake,
and the records are the shapes the API reference documents. The claims under test
are the reader half of ADR 0011:

1. **Read time applies the two reader attributes.** The query filters on
   ``eventName == StopInstances`` (the only ``LookupAttributes`` entry the API
   permits) and the reader filters ``eventSource == ec2.amazonaws.com`` in code.
   Nothing else is attributed here.
2. **A failure is never an absence.** A denied/throttled call, a malformed page,
   an unreadable ``StopInstances`` record, or an unusable window is
   ``available=False`` -- never an empty event tuple that reconciliation would
   have to read as "probably fine".
3. **The reader does not attribute or decide.** It returns every matching event,
   including ambiguous ones, so ``reconcile_dispatch`` can fail closed on two
   events rather than the reader quietly picking one.

The end-to-end assertions feed the reader's output straight into
``reconcile_dispatch``, which is the whole point of the wiring: the reader
retrieves, the reconciler judges, and neither does the other's job.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sws_agent.cloudtrail_reader as reader_module
from sws_agent.cloudtrail_reader import (
    CloudTrailEvidenceReader,
    LookupEventsEvidenceReader,
)
from sws_agent.reconciliation import (
    ReconciliationOutcome,
    reconcile_dispatch,
)

INSTANCE_ID = "i-0abcdef1234567890"
OTHER_INSTANCE_ID = "i-0fffffffffffffff0"
MUTATOR_ARN = "arn:aws:sts::123456789012:assumed-role/SwsStopRole/sws-first-stop"
FORENSIC_ARN = "arn:aws:sts::123456789012:assumed-role/SwsForensics/sws-audit"
OTHER_ACCOUNT_ARN = "arn:aws:sts::999999999999:assumed-role/SwsStopRole/sws-first-stop"
SESSION = "sws-first-stop"
ROLE_ID = "AROAEXAMPLE"
EC2 = "ec2.amazonaws.com"

START = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(seconds=30)


# -- a local LookupEvents seam -------------------------------------------------


class _Paginator:
    def __init__(self, seam: "_Seam") -> None:
        self._seam = seam

    def paginate(self, **params: object):
        self._seam.calls.append(params)
        if self._seam.raise_during_paginate is not None:
            raise self._seam.raise_during_paginate
        yield from self._seam.pages


class _Seam:
    """A fake client exposing ``lookup_events()`` -> paginator."""

    def __init__(
        self,
        pages: list[object] | None = None,
        *,
        raise_on_lookup: Exception | None = None,
        raise_during_paginate: Exception | None = None,
    ) -> None:
        self.pages = list(pages or [])
        self.raise_on_lookup = raise_on_lookup
        self.raise_during_paginate = raise_during_paginate
        self.calls: list[dict] = []

    def lookup_events(self) -> _Paginator:
        if self.raise_on_lookup is not None:
            raise self.raise_on_lookup
        return _Paginator(self)


def _record(
    *,
    event_id: str = "evt-1",
    event_name: str = "StopInstances",
    event_source: str = EC2,
    instance_ids: tuple[str, ...] = (INSTANCE_ID,),
    principal_arn: str | None = MUTATOR_ARN,
    session: str | None = SESSION,
    error_code: str | None = None,
    read_only: bool = False,
    event_time: object = "default",
    cloud_trail_event: object = "auto",
) -> dict:
    """Build one realistic LookupEvents ``Event`` wrapper."""
    record: dict = {
        "EventId": event_id,
        "EventName": event_name,
        "EventSource": event_source,
        "EventTime": (
            START + timedelta(seconds=5) if event_time == "default" else event_time
        ),
        "Username": f"{ROLE_ID}:{session}" if session else ROLE_ID,
        "Resources": [
            {"ResourceType": "Instance", "ResourceId": i} for i in instance_ids
        ],
    }
    if cloud_trail_event == "auto":
        payload: dict = {"readOnly": read_only, "userIdentity": {}}
        if principal_arn is not None:
            payload["userIdentity"]["arn"] = principal_arn
        payload["requestParameters"] = {
            "instancesSet": {"items": [{"instanceId": i} for i in instance_ids]}
        }
        if error_code is not None:
            payload["errorCode"] = error_code
        record["CloudTrailEvent"] = json.dumps(payload)
    elif cloud_trail_event is not None:
        record["CloudTrailEvent"] = cloud_trail_event
    return record


def _page(*records: dict) -> dict:
    return {"Events": list(records)}


def _reader(
    seam: _Seam, *, reader_arn: str = FORENSIC_ARN
) -> LookupEventsEvidenceReader:
    return LookupEventsEvidenceReader(
        seam, region="us-east-1", reader_principal_arn=reader_arn
    )


def _read(
    seam: _Seam,
    *,
    start: object = START,
    end: object = END,
    reader_arn: str = FORENSIC_ARN,
):
    return _reader(seam, reader_arn=reader_arn).read_stop_events(
        window_start=start, window_end=end
    )


def _reconcile(
    evidence,
    *,
    witness_attempts: int = 1,
    witness_instance_ids: tuple[str, ...] = (INSTANCE_ID,),
    instance_id: str = INSTANCE_ID,
    principal: str = MUTATOR_ARN,
    session: str | None = SESSION,
):
    return reconcile_dispatch(
        expected_instance_id=instance_id,
        expected_principal_arn=principal,
        window_start=START,
        window_end=END,
        witness_attempts=witness_attempts,
        witness_instance_ids=witness_instance_ids,
        cloudtrail=evidence,
        session_name=session,
    )


# -- the retrieval path, end to end -------------------------------------------


def test_a_matching_lookup_event_confirms_through_the_reconciler() -> None:
    evidence = _read(_Seam([_page(_record())]))
    assert evidence.available is True
    assert evidence.reader_principal_arn == FORENSIC_ARN
    assert len(evidence.events) == 1
    report = _reconcile(evidence)
    assert report.outcome is ReconciliationOutcome.CONFIRMED, report.reason()


def test_the_query_uses_one_event_name_attribute_and_the_window() -> None:
    """``LookupEvents`` permits exactly one ``LookupAttributes`` entry per call.

    The reader therefore asks for ``eventName == StopInstances`` and applies the
    ``eventSource`` filter itself; a second attribute would be rejected by AWS.
    """
    seam = _Seam([_page(_record())])
    _read(seam)
    assert len(seam.calls) == 1
    params = seam.calls[0]
    attributes = params["LookupAttributes"]
    assert isinstance(attributes, list) and len(attributes) == 1
    assert attributes[0] == {
        "AttributeKey": "EventName",
        "AttributeValue": "StopInstances",
    }
    assert params["StartTime"] == START
    assert params["EndTime"] == END


def test_the_reader_reads_every_page_not_just_the_first() -> None:
    seam = _Seam([_page(_record(event_id="e1")), _page(_record(event_id="e2"))])
    evidence = _read(seam)
    assert {event.event_id for event in evidence.events} == {"e1", "e2"}


# -- the two reader attributes -------------------------------------------------


def test_a_non_ec2_stopinstances_record_is_filtered_out() -> None:
    evidence = _read(_Seam([_page(_record(event_source="not-ec2.amazonaws.com"))]))
    assert evidence.available is True
    assert evidence.events == ()
    report = _reconcile(evidence)
    assert report.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT


def test_a_non_stopinstances_record_is_filtered_out() -> None:
    evidence = _read(_Seam([_page(_record(event_name="StartInstances"))]))
    assert evidence.events == ()
    report = _reconcile(evidence)
    assert report.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT


def test_a_rejected_call_still_counts_as_one_dispatch() -> None:
    """An ``errorCode`` means AWS did not apply it, but it was still one event."""
    evidence = _read(_Seam([_page(_record(error_code="UnauthorizedOperation"))]))
    assert evidence.events[0].error_code == "UnauthorizedOperation"
    report = _reconcile(evidence)
    assert report.outcome is ReconciliationOutcome.CONFIRMED, report.reason()
    assert report.aws_side_count == 1


# -- the reader does not attribute: the reconciler does ------------------------


def test_an_event_for_another_instance_is_not_attributed() -> None:
    """The reader returns it -- it does not filter targets -- and the reconciler
    refuses to count it, because the target does not match."""
    evidence = _read(_Seam([_page(_record(instance_ids=(OTHER_INSTANCE_ID,)))]))
    assert len(evidence.events) == 1
    report = _reconcile(evidence)
    assert report.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT
    assert report.aws_side_count == 0


def test_an_event_from_another_account_is_not_attributed() -> None:
    report = _reconcile(_read(_Seam([_page(_record(principal_arn=OTHER_ACCOUNT_ARN))])))
    assert report.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT
    finding = next(f for f in report.failures if f.check == "principal_attribution")
    assert "different principal" in finding.detail


def test_an_event_from_another_session_is_not_attributed() -> None:
    report = _reconcile(_read(_Seam([_page(_record(session="some-other-session"))])))
    assert not report.confirmed
    finding = next(f for f in report.failures if f.check == "principal_attribution")
    assert "different principal or session" in finding.detail


def test_two_matching_events_fail_closed_as_ambiguous() -> None:
    """The reader returns both; the reconciler refuses to pick one."""
    evidence = _read(_Seam([_page(_record(event_id="e1"), _record(event_id="e2"))]))
    assert len(evidence.events) == 2
    report = _reconcile(evidence)
    assert report.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT
    assert report.aws_side_count == 2


def test_the_reader_records_its_principal_and_a_self_read_is_refused() -> None:
    """The credential that can change things must not be able to inspect itself."""
    evidence = _read(_Seam([_page(_record())]), reader_arn=MUTATOR_ARN)
    assert evidence.reader_principal_arn == MUTATOR_ARN
    report = _reconcile(evidence)
    assert not report.confirmed
    finding = next(f for f in report.failures if f.check == "evidence_provenance")
    assert not finding.passed


# -- an empty result is not automatically a pass -------------------------------


def test_a_wrong_region_lookup_is_available_and_empty_but_not_a_pass() -> None:
    """A client bound to the wrong region returns clean and empty, forever.

    The reader reports that honestly (available, zero events); the reconciler
    then refuses to confirm a dispatch the witness claims happened.
    """
    evidence = _read(_Seam([_page()]))
    assert evidence.available is True
    assert evidence.events == ()
    report = _reconcile(evidence)
    assert report.outcome is ReconciliationOutcome.AWS_SIDE_DISAGREEMENT
    assert not report.confirmed


def test_no_events_and_no_witness_is_a_verified_absence() -> None:
    report = _reconcile(
        _read(_Seam([_page()])), witness_attempts=0, witness_instance_ids=()
    )
    assert report.outcome is ReconciliationOutcome.NO_DISPATCH_OBSERVED
    assert report.retry_permissible is True


# -- a failure is never an absence ---------------------------------------------


def test_a_failed_lookup_is_unresolved_never_an_absent_dispatch() -> None:
    seam = _Seam(raise_on_lookup=RuntimeError("AccessDeniedException"))
    evidence = _read(seam)
    assert evidence.available is False
    assert evidence.error is not None
    # The AWS exception text must not be propagated; only the failure kind is.
    assert "RuntimeError" in evidence.error
    assert "AccessDeniedException" not in evidence.error
    report = _reconcile(evidence)
    assert report.outcome is ReconciliationOutcome.UNRESOLVED
    assert report.outcome is not ReconciliationOutcome.NO_DISPATCH_OBSERVED
    assert report.retry_permissible is False


def test_a_failure_while_draining_pages_fails_the_whole_query() -> None:
    """Partial data is not usable evidence: a mid-drain failure is unavailable."""
    seam = _Seam([_page(_record())], raise_during_paginate=RuntimeError("throttled"))
    evidence = _read(seam)
    assert evidence.available is False
    assert evidence.events == ()


def test_a_malformed_page_fails_closed() -> None:
    evidence = _read(_Seam(["not-a-page"]))
    assert evidence.available is False
    assert _reconcile(evidence).outcome is ReconciliationOutcome.UNRESOLVED


def test_a_malformed_cloudtrail_event_fails_closed() -> None:
    evidence = _read(_Seam([_page(_record(cloud_trail_event="{not json"))]))
    assert evidence.available is False
    assert _reconcile(evidence).outcome is ReconciliationOutcome.UNRESOLVED


def test_a_record_without_a_timestamp_fails_closed() -> None:
    record = _record()
    del record["EventTime"]
    evidence = _read(_Seam([_page(record)]))
    assert evidence.available is False


def test_a_stopinstances_record_with_no_readable_target_fails_closed() -> None:
    """A StopInstances record whose targets cannot be read is not usable evidence."""
    evidence = _read(_Seam([_page(_record(instance_ids=()))]))
    assert evidence.available is False


def test_an_unusable_window_is_refused_before_any_query() -> None:
    seam = _Seam([_page(_record())])
    evidence = _read(seam, start=datetime(2026, 10, 4, 12, 0, 0))  # naive
    assert evidence.available is False
    assert seam.calls == [], "a naive window must be refused before the query runs"


# -- field fidelity against the AWS shape --------------------------------------


def test_instance_ids_come_from_request_parameters_not_the_resource_summary() -> None:
    """``RequestParameters`` is the authoritative statement of what was requested.

    Here the ``Resources`` summary and the request disagree; the request wins.
    """
    payload = {
        "readOnly": False,
        "userIdentity": {"arn": MUTATOR_ARN},
        "requestParameters": {"instancesSet": {"items": [{"instanceId": INSTANCE_ID}]}},
    }
    record = _record(
        instance_ids=(OTHER_INSTANCE_ID,),
        cloud_trail_event=json.dumps(payload),
    )
    evidence = _read(_Seam([_page(record)]))
    assert evidence.events[0].instance_ids == (INSTANCE_ID,)


def test_the_session_name_is_recovered_from_username() -> None:
    evidence = _read(_Seam([_page(_record())]))
    assert evidence.events[0].session_name == SESSION
    assert evidence.events[0].principal_arn == MUTATOR_ARN


def test_read_only_is_read_from_the_record() -> None:
    evidence = _read(_Seam([_page(_record(read_only=True))]))
    assert evidence.events[0].read_only is True


# -- construction guards -------------------------------------------------------


def test_the_concrete_reader_satisfies_the_protocol() -> None:
    assert isinstance(_reader(_Seam()), CloudTrailEvidenceReader)


def test_the_reader_refuses_an_anonymous_principal() -> None:
    with pytest.raises(ValueError):
        LookupEventsEvidenceReader(
            _Seam(), region="us-east-1", reader_principal_arn="   "
        )


def test_the_reader_refuses_a_blank_region() -> None:
    with pytest.raises(ValueError):
        LookupEventsEvidenceReader(
            _Seam(), region="", reader_principal_arn=FORENSIC_ARN
        )


# -- the security boundary -----------------------------------------------------


def test_the_reader_never_dispatches_or_imports_the_aws_sdk() -> None:
    """AST-based, because a text scan cannot tell a name from a call.

    The reader must contain no mutating operation and no AWS SDK import: the
    transport stays behind the injected seam, exactly as M11's provider keeps it.
    """
    source = Path(reader_module.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]
    for forbidden in ("boto3", "botocore"):
        assert forbidden not in body, f"the reader must not contain {forbidden!r}"
    tree = ast.parse(source)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                called.add(func.attr)
            elif isinstance(func, ast.Name):
                called.add(func.id)
    for forbidden in (
        "stop_instances",
        "start_instances",
        "terminate_instances",
        "reboot_instances",
        "modify_instance_attribute",
    ):
        assert forbidden not in called, f"the reader must not call {forbidden!r}"


def test_the_reader_imports_no_mutation_or_authorization_module() -> None:
    """Retrieval must not reach the mutation or the pre-dispatch gate.

    Importing the mutation stack would couple the read-only reader to the thing
    it verifies; importing preflight would reintroduce the ADR 0011 circularity.
    """
    source = Path(reader_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("mutation" in name for name in imported)
    assert not any("preflight" in name for name in imported)


def test_the_reader_is_not_reachable_from_the_mcp_surface_or_preflight() -> None:
    import sws_agent.mcp.server as server
    import sws_agent.preflight as preflight

    for module in (server, preflight):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "cloudtrail_reader" not in source
        assert "CloudTrailEvidenceReader" not in source


def test_stop_resource_remains_unimplemented() -> None:
    from sws_agent.constants import PotentialAction
    from sws_agent.execution import ACTION_EXECUTION_REGISTRY

    assert ACTION_EXECUTION_REGISTRY[PotentialAction.STOP_RESOURCE].implemented is False


def test_the_mcp_surface_stays_at_nine_tools() -> None:
    from sws_agent.mcp.server import BUILTIN_TOOL_NAMES

    assert len(BUILTIN_TOOL_NAMES) == 9
