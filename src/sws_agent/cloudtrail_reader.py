"""Read-only forensic CloudTrail retrieval (M16).

ADR 0011 split CloudTrail verification into two halves and kept them apart on
purpose: :mod:`sws_agent.reconciliation` *judges* evidence it is handed, and this
module is the missing half that *retrieves* it. The separation is not stylistic.
A single module that both queried and judged could quietly retry, widen the
window, or drop an event, and none of that would show up in the verdict (ADR 0011,
"What the module deliberately does not do").

This module is therefore a reader and nothing else:

- It performs exactly one logical CloudTrail ``LookupEvents`` operation, drains
  every page, and normalizes each record into the shapes reconciliation already
  consumes (``CloudTrailStopEvent`` / ``CloudTrailEvidence``). It does not count,
  attribute, or decide.
- It applies, at read time, the **two** attributes ADR 0011 assigns to the
  reader: ``eventName == StopInstances`` and
  ``eventSource == ec2.amazonaws.com``. ``LookupEvents`` accepts only **one**
  ``LookupAttributes`` entry per call, so ``eventName`` is applied by the query
  and ``eventSource`` is applied in code; both are the reader's responsibility.
  The remaining four attributes -- principal, session, target, and window -- are
  applied by :func:`sws_agent.reconciliation.reconcile_dispatch`, which is why
  this reader returns *every* matching event rather than a single best candidate.
  Selecting one here would hide the ambiguity the reconciler exists to catch.
- It never dispatches. There is no code path here that calls ``StopInstances``
  (or any mutation), builds a mutating client, or touches EC2 at all. It imports
  no AWS SDK: the ``boto3``/``botocore`` transport stays behind the injected
  client seam, exactly as
  :class:`sws_agent.ec2_observation.Ec2InstanceObservationProvider` keeps it.
  There is deliberately no MCP tool and no dispatch path reachable from here.

Failure is never inferred as absence. Every way the query can fail -- a denied,
throttled, or timed-out call; a malformed page; a ``StopInstances`` record whose
targets cannot be read; an unusable window -- becomes
``CloudTrailEvidence(available=False, error=...)``. ``available`` is what stops a
failed lookup from being read as "no events", which reconciliation would otherwise
have to read as "probably fine". A reader that returned an empty event tuple on
error would be the worst kind of verification step: one that looks like a pass.

Delivery maturity is deliberately *not* enforced here. CloudTrail delivery is
asynchronous, so an early query can return a clean empty result for a dispatch
that was in fact processed. The runbook's Policy A (bounded wait and re-query) is
procedural and lives in the runbook, not in this module; a retry loop baked in
here would make the reader quietly authoritative about timing and would contradict
ADR 0011's insistence that retrieval and judgement stay separate and visible.

Seam contract (mirrors the M11 observation provider): the injected ``client``
exposes ``lookup_events()`` returning the ``cloudtrail:LookupEvents`` paginator.
A raw boto3 client exposes ``get_paginator("lookup_events")`` instead, so the
composition root supplies the one-method adapter, as ``sws_live`` does for
``describe_instances``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .reconciliation import (
    EC2_EVENT_SOURCE,
    STOP_INSTANCES_EVENT_NAME,
    CloudTrailEvidence,
    CloudTrailStopEvent,
)

__all__ = [
    "CloudTrailEvidenceReader",
    "CloudTrailReaderError",
    "LookupEventsEvidenceReader",
]


class CloudTrailReaderError(RuntimeError):
    """A response could not be faithfully normalized into evidence.

    Raised from inside the reader while turning an AWS record into a
    ``CloudTrailStopEvent``. It is caught by :meth:`LookupEventsEvidenceReader.read_stop_events`
    and surfaced as ``CloudTrailEvidence(available=False, ...)`` rather than
    propagating: a record we cannot read is "unverified", never "no event".
    """


@runtime_checkable
class CloudTrailEvidenceReader(Protocol):
    """Retrieves CloudTrail ``StopInstances`` evidence for a time window.

    The window is required and timezone-aware. Nothing else -- no principal, no
    target -- is a parameter, because the reader does not attribute: it returns
    every ``StopInstances``/EC2 event in the window and lets
    :func:`sws_agent.reconciliation.reconcile_dispatch` decide what it means.
    """

    def read_stop_events(
        self, *, window_start: datetime, window_end: datetime
    ) -> CloudTrailEvidence: ...


class LookupEventsEvidenceReader:
    """A read-only ``LookupEvents`` reader producing ``CloudTrailEvidence``.

    Constructed with an injected client seam exposing ``lookup_events()`` (which
    returns the ``cloudtrail:LookupEvents`` paginator), the region the client is
    bound to, and the ARN of the principal that ran the query. The reader records
    that ARN on the evidence so reconciliation can refuse evidence whose reader is
    the mutating principal -- the credential that can change things must not be
    able to inspect itself.
    """

    def __init__(
        self,
        client: Any,
        *,
        region: str,
        reader_principal_arn: str,
    ) -> None:
        if not isinstance(region, str) or not region.strip():
            raise ValueError(
                "CloudTrail retrieval requires an explicit region binding; "
                "LookupEvents is regional, and a wrong-region query returns clean "
                "and empty forever"
            )
        if (
            not isinstance(reader_principal_arn, str)
            or not reader_principal_arn.strip()
        ):
            raise ValueError(
                "CloudTrail retrieval requires the reader's own principal ARN; an "
                "anonymous read cannot be recorded as independent evidence"
            )
        self._client = client
        self._region = region
        self._reader_principal_arn = reader_principal_arn

    @property
    def region(self) -> str:
        return self._region

    def read_stop_events(
        self, *, window_start: datetime, window_end: datetime
    ) -> CloudTrailEvidence:
        """Query once for ``StopInstances`` events in the window, read-only.

        Returns ``available=True`` with every matching event when the query
        succeeded (possibly zero events), and ``available=False`` with a reason
        when it did not. It never raises for an AWS or shape failure and never
        converts a failure into an empty tuple.
        """
        if not _usable_window(window_start, window_end):
            return self._unavailable(
                "the correlation window is missing, timezone-naive, or inverted, "
                "so events cannot be correlated in time and no evidence can be "
                "attributed"
            )
        params: dict[str, Any] = {
            "LookupAttributes": [
                {
                    "AttributeKey": "EventName",
                    "AttributeValue": STOP_INSTANCES_EVENT_NAME,
                }
            ],
            "StartTime": window_start,
            "EndTime": window_end,
        }
        events: list[CloudTrailStopEvent] = []
        try:
            paginator = self._client.lookup_events()
            for page in paginator.paginate(**params):
                events.extend(_events_from_page(page))
        except CloudTrailReaderError as exc:
            return self._unavailable(str(exc))
        except Exception as exc:  # noqa: BLE001 - any AWS/transport failure is a refusal
            # The AWS exception's own message is intentionally not propagated: it
            # can carry request identifiers and request detail that must not reach
            # a durable record. The type name is enough to report the failure kind.
            return self._unavailable(f"LookupEvents failed: {type(exc).__name__}")
        return CloudTrailEvidence(
            available=True,
            events=tuple(events),
            reader_principal_arn=self._reader_principal_arn,
        )

    def _unavailable(self, reason: str) -> CloudTrailEvidence:
        return CloudTrailEvidence(
            available=False,
            events=(),
            error=reason,
            reader_principal_arn=self._reader_principal_arn,
        )


# -- normalization ------------------------------------------------------------


def _usable_window(start: object, end: object) -> bool:
    """Both bounds must be timezone-aware and strictly ordered.

    A naive timestamp cannot be correlated, and assuming UTC for it would be a
    guess in the one place a guess is least affordable (ADR 0011).
    """
    return (
        isinstance(start, datetime)
        and isinstance(end, datetime)
        and start.tzinfo is not None
        and end.tzinfo is not None
        and start < end
    )


def _events_from_page(page: object) -> list[CloudTrailStopEvent]:
    """Normalize one LookupEvents page, keeping only EC2 ``StopInstances`` records.

    Records are filtered on their raw ``EventName``/``EventSource`` *before*
    normalization, so an unrelated record is skipped rather than mistaken for a
    malformed one. A record that does claim to be an EC2 ``StopInstances`` event
    and cannot then be read is a normalization failure and fails the whole query
    closed: silently dropping it could undercount and manufacture a confirmation.
    """
    if not isinstance(page, Mapping):
        raise CloudTrailReaderError(
            f"a LookupEvents page is not an object: {type(page).__name__}"
        )
    records = page.get("Events")
    if records is None:
        return []
    if not isinstance(records, list):
        raise CloudTrailReaderError(
            f"a LookupEvents page carries malformed Events: {type(records).__name__}"
        )
    out: list[CloudTrailStopEvent] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise CloudTrailReaderError(
                f"a LookupEvents record is not an object: {type(record).__name__}"
            )
        if (
            record.get("EventName") != STOP_INSTANCES_EVENT_NAME
            or record.get("EventSource") != EC2_EVENT_SOURCE
        ):
            continue
        out.append(_normalize_event(record))
    return out


def _normalize_event(record: Mapping[str, Any]) -> CloudTrailStopEvent:
    """Turn one EC2 ``StopInstances`` LookupEvents record into a stop event.

    Field names come from the CloudTrail API reference. The authoritative statement
    of what was *requested* is ``CloudTrailEvent.requestParameters.instancesSet``;
    the enclosing ``Resources`` list is only a fallback, because it is a summary of
    affected resources rather than the request in the caller's words.
    """
    event_id = _require_text(record.get("EventId"), "EventId")
    event_name = _require_text(record.get("EventName"), "EventName")
    event_source = _require_text(record.get("EventSource"), "EventSource")
    event_time = _require_datetime(record.get("EventTime"), "EventTime")
    payload = _cloud_trail_event(record.get("CloudTrailEvent"))

    identity = payload.get("userIdentity")
    identity = identity if isinstance(identity, Mapping) else {}
    principal_arn = _optional_text(identity.get("arn"))
    session_name = _session_from_username(
        _optional_text(record.get("Username"))
    ) or _session_from_arn(principal_arn)

    instance_ids = _instance_ids_from_payload(payload) or _instance_ids_from_resources(
        record.get("Resources")
    )
    if not instance_ids:
        raise CloudTrailReaderError(
            f"StopInstances event {event_id} carries no readable instance ids; a "
            "record whose targets cannot be established is not usable evidence"
        )

    return CloudTrailStopEvent(
        event_id=event_id,
        event_time=event_time,
        event_name=event_name,
        event_source=event_source,
        principal_arn=principal_arn,
        session_name=session_name,
        instance_ids=instance_ids,
        error_code=_optional_text(payload.get("errorCode")),
        read_only=_read_only(payload, record.get("ReadOnly")),
    )


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CloudTrailReaderError(
            f"a CloudTrail record is missing a non-empty {label} "
            f"(got {type(value).__name__})"
        )
    return value


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _require_datetime(value: object, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CloudTrailReaderError(
                f"a CloudTrail record's {label} is not a valid timestamp"
            ) from exc
    else:
        raise CloudTrailReaderError(
            f"a CloudTrail record has no {label} timestamp (got {type(value).__name__})"
        )
    if parsed.tzinfo is None:
        raise CloudTrailReaderError(
            f"a CloudTrail record's {label} is timezone-naive and cannot be "
            "correlated in time"
        )
    return parsed


def _cloud_trail_event(raw: object) -> dict[str, Any]:
    """Parse the ``CloudTrailEvent`` field, which is a JSON *string*.

    An absent field yields ``{}`` (the record is still usable from its top-level
    fields). A present field that is not a JSON object is a shape we do not
    understand and fails closed.
    """
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise CloudTrailReaderError(
                "a CloudTrail record's CloudTrailEvent is not valid JSON"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise CloudTrailReaderError(
                "a CloudTrail record's CloudTrailEvent is not a JSON object"
            )
        return dict(parsed)
    raise CloudTrailReaderError(
        f"a CloudTrail record's CloudTrailEvent is malformed: {type(raw).__name__}"
    )


def _instance_ids_from_payload(payload: Mapping[str, Any]) -> tuple[str, ...]:
    items = _dig(payload, ("requestParameters", "instancesSet", "items"))
    ids: list[str] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, Mapping):
                instance_id = _optional_text(item.get("instanceId"))
                if instance_id is not None:
                    ids.append(instance_id)
    return tuple(ids)


def _instance_ids_from_resources(resources: object) -> tuple[str, ...]:
    ids: list[str] = []
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, Mapping):
                continue
            if resource.get("ResourceType") not in ("Instance", "AWS::EC2::Instance"):
                continue
            instance_id = _optional_text(resource.get("ResourceId")) or _optional_text(
                resource.get("ResourceName")
            )
            if instance_id is not None:
                ids.append(instance_id)
    return tuple(ids)


def _read_only(payload: Mapping[str, Any], top_level: object) -> bool:
    flag = payload.get("readOnly")
    if isinstance(flag, bool):
        return flag
    if isinstance(top_level, str):
        return top_level.strip().lower() == "true"
    return False


def _session_from_username(username: str | None) -> str | None:
    """``Username`` is ``ROLE_ID:session``; the session is what identifies a run."""
    if username is None:
        return None
    _, separator, session = username.partition(":")
    if not separator:
        return None
    return _optional_text(session)


def _session_from_arn(arn: str | None) -> str | None:
    """The last ``/`` segment of an assumed-role ARN is the session name."""
    if arn is None:
        return None
    return _optional_text(arn.rsplit("/", 1)[-1])


def _dig(source: object, path: tuple[str, ...]) -> Any:
    current: Any = source
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current
