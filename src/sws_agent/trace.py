"""Execution-event tracing for SWS.

REUSE DECISION (see docs/reuse-decisions.md):

- Reused (pattern): SMS's ordered, append-only execution-event sink and
  its truthfulness contract -- a recorder records real events with real
  statuses and never fabricates a successful action, an AWS state change,
  or a verification result -- plus ``to_dicts`` / ``from_dicts``
  serialization.
- Rewritten: the event vocabulary. SMS's stage names are replaced with
  SWS event types (see ``constants.TraceEventType``).

SWS trace events never contain chain-of-thought or reasoning text.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .constants import MAX_TRACE_EVENTS, TraceEventType, TraceStatus


class TraceCapacityError(RuntimeError):
    """Raised when a recorder has already recorded its maximum events."""


@dataclass(frozen=True)
class TraceEvent:
    """A single, immutable execution event."""

    event_id: int
    event_type: TraceEventType
    status: TraceStatus
    message: str
    timestamp: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "status": self.status.value,
            "message": self.message,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TraceEvent":
        return cls(
            event_id=int(data["event_id"]),
            event_type=TraceEventType(data["event_type"]),
            status=TraceStatus(data["status"]),
            message=data["message"],
            timestamp=data["timestamp"],
            metadata=dict(data.get("metadata") or {}),
        )


class TraceRecorder:
    """Append-only, ordered collection of execution events.

    The recorder only stores events the caller records. It never invents
    or auto-completes events: a transition from RUNNING to SUCCEEDED is
    only possible when the caller records it based on a real outcome.
    """

    def __init__(
        self,
        max_events: int = MAX_TRACE_EVENTS,
        seed_events: Iterable[TraceEvent] | None = None,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be a positive integer")
        seeded = list(seed_events or [])
        if len(seeded) > max_events:
            raise TraceCapacityError(
                f"seed exceeds max_events ({len(seeded)} > {max_events})"
            )
        self._max_events: int = max_events
        self._events: list[TraceEvent] = list(seeded)
        self._sequence = itertools.count(max((e.event_id for e in seeded), default=0) + 1)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _append(
        self,
        event_type: TraceEventType,
        status: TraceStatus,
        message: str,
        metadata: dict[str, Any] | None,
    ) -> TraceEvent:
        if len(self._events) >= self._max_events:
            raise TraceCapacityError(f"trace buffer is full ({self._max_events} events)")
        event = TraceEvent(
            event_id=next(self._sequence),
            event_type=event_type,
            status=status,
            message=message,
            timestamp=self._now_iso(),
            metadata=dict(metadata or {}),
        )
        self._events.append(event)
        return event

    def record(
        self,
        event_type: TraceEventType,
        message: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        """Append a RUNNING event for work that has begun."""
        return self._append(event_type, TraceStatus.RUNNING, message, metadata)

    def succeed(
        self,
        event_type: TraceEventType,
        message: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        """Append an event reflecting a verified successful outcome."""
        return self._append(event_type, TraceStatus.SUCCEEDED, message, metadata)

    def fail(
        self,
        event_type: TraceEventType,
        message: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        """Append an event reflecting a real failure."""
        return self._append(event_type, TraceStatus.FAILED, message, metadata)

    def wait(
        self,
        event_type: TraceEventType,
        message: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        """Append an event reflecting a required wait (e.g. human approval)."""
        return self._append(event_type, TraceStatus.WAITING, message, metadata)

    def __iter__(self):
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def to_dicts(self) -> list[dict[str, Any]]:
        """Serialize all recorded events, in order."""
        return [event.to_dict() for event in self._events]

    @classmethod
    def from_dicts(cls, dicts: Iterable[dict[str, Any]]) -> "TraceRecorder":
        """Rebuild a recorder from serialized events."""
        events = [TraceEvent.from_dict(data) for data in dicts]
        recorder = cls(max_events=MAX_TRACE_EVENTS, seed_events=events)
        return recorder