"""Trace recording: the truthfulness contract.

Tests pin that a recorder is append-only, ordered, never fabricates a
successful outcome, and serializes deterministically.
"""

from __future__ import annotations

import pytest

from sws_agent.constants import MAX_TRACE_EVENTS, TraceEventType, TraceStatus
from sws_agent.trace import TraceCapacityError, TraceEvent, TraceRecorder


def _recorder() -> TraceRecorder:
    return TraceRecorder()


def test_record_appends_running_event():
    recorder = _recorder()
    event = recorder.record(TraceEventType.INVENTORY_QUERY, "started inventory")
    assert len(recorder) == 1
    assert event.status is TraceStatus.RUNNING
    assert event.event_type is TraceEventType.INVENTORY_QUERY
    assert event.message == "started inventory"


def test_events_are_append_only_and_ordered():
    recorder = _recorder()
    first = recorder.record(TraceEventType.REQUEST_RECEIVED, "request received")
    second = recorder.succeed(TraceEventType.INVENTORY_QUERY, "inventory done")
    third = recorder.fail(TraceEventType.VERIFICATION, "verification failed")

    assert first.event_id < second.event_id < third.event_id
    assert [e.event_id for e in recorder] == [first.event_id, second.event_id, third.event_id]


def test_terminal_status_helpers_set_expected_statuses():
    recorder = _recorder()
    recorder.succeed(TraceEventType.ACTION_EXECUTION, "ok")
    recorder.fail(TraceEventType.ACTION_EXECUTION, "failed")
    recorder.wait(TraceEventType.APPROVAL_FLOW, "waiting for approval")
    statuses = [e.status for e in recorder]
    assert statuses == [TraceStatus.SUCCEEDED, TraceStatus.FAILED, TraceStatus.WAITING]


def test_recorder_never_fabricates_success():
    """Recording a RUNNING event must never auto-complete it."""
    recorder = _recorder()
    recorder.record(TraceEventType.ACTION_EXECUTION, "action started")
    assert [e.status for e in recorder] == [TraceStatus.RUNNING]


def test_to_dicts_from_dicts_roundtrip():
    recorder = _recorder()
    recorder.record(TraceEventType.REQUEST_RECEIVED, "hello", metadata={"k": "v"})
    recorder.succeed(TraceEventType.AUDIT, "recorded")
    serialized = recorder.to_dicts()

    rebuilt = TraceRecorder.from_dicts(serialized)
    assert rebuilt.to_dicts() == serialized
    # Sequence continues after rebuild rather than resetting.
    assert len(rebuilt) == 2


def test_from_dicts_continues_event_id_sequence():
    recorder = _recorder()
    recorder.record(TraceEventType.REQUEST_RECEIVED, "a")
    recorder.record(TraceEventType.REQUEST_RECEIVED, "b")
    rebuilt = TraceRecorder.from_dicts(recorder.to_dicts())
    next_event = rebuilt.record(TraceEventType.INVENTORY_QUERY, "c")
    assert next_event.event_id == 3


def test_capacity_limit_is_enforced():
    recorder = TraceRecorder(max_events=2)
    recorder.record(TraceEventType.REQUEST_RECEIVED, "one")
    recorder.record(TraceEventType.REQUEST_RECEIVED, "two")
    with pytest.raises(TraceCapacityError):
        recorder.record(TraceEventType.REQUEST_RECEIVED, "three")


def test_invalid_max_events_rejected():
    with pytest.raises(ValueError):
        TraceRecorder(max_events=0)


def test_to_dict_contains_canonical_vocabulary_values():
    recorder = _recorder()
    recorder.succeed(TraceEventType.POLICY_EVALUATION, "evaluated")
    data = recorder.to_dicts()[0]
    assert data["event_type"] == TraceEventType.POLICY_EVALUATION.value
    assert data["status"] == TraceStatus.SUCCEEDED.value
    assert data["event_id"] == 1


def test_trace_event_roundtrip_metadata_copied():
    event = TraceEvent(
        event_id=1,
        event_type=TraceEventType.ANALYSIS,
        status=TraceStatus.RUNNING,
        message="analyzing",
        timestamp="2026-01-01T00:00:00+00:00",
        metadata={"resource": "bucket-a"},
    )
    rebuilt = TraceEvent.from_dict(event.to_dict())
    assert rebuilt.metadata == {"resource": "bucket-a"}
    assert rebuilt.event_type is TraceEventType.ANALYSIS