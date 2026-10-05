"""Evidence that exactly one StopInstances request reached the wire.

M15-F's central invariant is not "the instance stopped". It is:

    The live experiment must prove not merely that the instance stopped, but that
    the entire safety chain prevented an unintended second dispatch.

That claim is about a *negative* -- something that must not have happened -- so a
counter reading of the evidence matters more than a confirming one. A witness
that only ever reports "one dispatch" is worthless: it would report one whether
or not the SDK had quietly retried, which is precisely the failure it exists to
catch. The same discipline M15-E applied to the retry configuration applies here.

So this module is built to be falsifiable, and :mod:`tests.test_m15f_dispatch_witness`
demonstrates the falsification rather than assuming it:

- On a correctly configured client, one ``StopInstances`` call produces exactly
  one recorded attempt.
- On a client that *does* retry, the same witness records two or more attempts and
  reports the violation.

The measurement basis is botocore's own event pipeline, not a counter SWS
increments next to its call site. ``before-send`` fires once per HTTP attempt, so
it is the last point before bytes leave. The SDK's own retry decision surfaces at
``needs-retry``, and each attempt carries an ``amz-sdk-request`` header of the
form ``attempt=N; max=M`` -- ``max=1`` is the service-side statement that no
further attempt is permitted. These are three independent observations of the
same fact, which is what makes disagreement between them meaningful.

This module sits behind the same hermeticity boundary as
:mod:`sws_agent.ec2_mutation_client`: it may touch botocore, and it must not be
imported by the core. Nothing here dispatches anything; it only observes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sws_agent.ec2_mutation import MutationClientConfigurationError

__all__ = [
    "DispatchRecord",
    "DispatchWitness",
    "SingleDispatchViolation",
    "STOP_INSTANCES_OPERATION",
]

STOP_INSTANCES_OPERATION = "StopInstances"

#: botocore names events ``<phase>.<service-id>.<operation>``
#: (``endpoint.py:277``), and the service id is hyphenated, so the observation
#: hooks are ``before-send.ec2.StopInstances`` and
#: ``needs-retry.ec2.StopInstances``. Registering without the service segment
#: silently attaches to nothing -- which is why :meth:`DispatchWitness.arm`
#: refuses clients it cannot observe rather than trusting a successful
#: ``register()`` call.
EC2_SERVICE_ID = "ec2"


class SingleDispatchViolation(RuntimeError):
    """More than one HTTP attempt reached the wire for one ``handle()`` call."""


@dataclass(frozen=True)
class DispatchRecord:
    """One observed HTTP attempt.

    ``attempt`` comes from the SDK's ``amz-sdk-request`` header, and is the
    attempt number the SDK itself stamped on the request. ``max_attempts`` is
    populated only when botocore declared a budget.

    ``max_attempts is None`` therefore does **not** mean "budget unknown, assume
    safe". Per ``botocore/handlers.py:1091-1103`` the ``max`` token is emitted
    only when present in the retries context, and botocore seeds it via
    ``MaxAttemptsSeeder`` only when a max was resolved. An absent ``max`` with
    ``total_max_attempts=1`` is the expected shape for a single-attempt client,
    so this is treated as "one attempt permitted" -- and the retry that would
    contradict it would show up as ``attempt=2`` regardless.

    This asymmetry is deliberate and is the reason the falsification test exists:
    the absence of a signal must not be read as a pass without a second,
    independent signal to corroborate it.
    """

    operation: str
    invocation_id: str | None
    attempt: int | None
    max_attempts: int | None
    instance_ids: tuple[str, ...]
    captured_during: str

    @property
    def target_count(self) -> int:
        return len(self.instance_ids)


@dataclass
class DispatchWitness:
    """Records every ``StopInstances`` attempt that reaches botocore's sender.

    Attach with :meth:`arm`. Registration is on operation-specific events, so
    observation of the mutation cannot be perturbed by unrelated API calls the
    same client might make.

    Two hooks are used because neither alone is sufficient, which was established
    by inspecting what botocore actually passes at each event:

    - ``provide-client-params.ec2.StopInstances`` carries ``params``, including
      ``InstanceIds``. This is where the target is read.
    - ``before-send.ec2.StopInstances`` carries the prepared ``request`` and its
      ``amz-sdk-request`` header. This is where attempt numbering is read, and it
      fires once per HTTP attempt -- the last observation point before bytes
      leave.

    Reading the target from ``before-send`` alone would be wrong: at that phase
    the parameters are already serialized and ``params`` is absent, so the target
    appears only in the request body, which is protocol-dependent (EC2's query
    protocol uses ``InstanceId.1=...``).

    The witness is passive: it registers listeners and reads. It has no code path
    that issues a request, so arming it cannot itself dispatch anything.
    """

    records: list[DispatchRecord] = field(default_factory=list)
    retry_questions: list[dict[str, Any]] = field(default_factory=list)
    _armed: bool = field(default=False, repr=False)
    _pending_instance_ids: tuple[str, ...] = field(default=(), repr=False)

    # -- attachment ----------------------------------------------------------

    def arm(
        self,
        client: Any,
        *,
        operation: str = STOP_INSTANCES_OPERATION,
        service_id: str = EC2_SERVICE_ID,
    ) -> None:
        """Register the observation hooks on ``client``.

        Refuses a client it cannot observe, because a witness that silently
        attached to nothing would produce an empty record indistinguishable from
        "no dispatch" -- the exact ambiguity this exists to remove.
        """
        events = getattr(getattr(client, "meta", None), "events", None)
        if events is None or not hasattr(events, "register"):
            raise MutationClientConfigurationError(
                "the client exposes no botocore event emitter, so a dispatch "
                "cannot be witnessed; arming is refused rather than producing an "
                "empty record that would read as 'nothing was sent'"
            )
        emitter = events
        emitter.register(
            f"provide-client-params.{service_id}.{operation}",
            self._on_params,
            unique_id="sws-dispatch-witness-params",
        )
        emitter.register(
            f"before-send.{service_id}.{operation}",
            self._on_before_send,
            unique_id="sws-dispatch-witness-before-send",
        )
        emitter.register(
            f"needs-retry.{service_id}.{operation}",
            self._on_needs_retry,
            unique_id="sws-dispatch-witness-needs-retry",
        )
        self._verify_registered(
            emitter,
            events,
            service_id=service_id,
            operation=operation,
        )
        self._armed = True

    def _verify_registered(
        self,
        emitter: Any,
        events: Any,
        *,
        service_id: str,
        operation: str,
    ) -> None:
        """Prove each listener is reachable, not merely that ``register`` returned.

        ``register()`` accepts any string. An event name botocore will never emit
        -- a typo, a renamed event, a future API shape -- is stored and then never
        fired, and the witness would report zero attempts for a run in which one
        dispatch occurred. That is the precise failure this class exists to
        prevent, so attachment is confirmed against the emitter's own registry
        before ``armed`` is set.

        A client whose emitter does not expose a searchable registry is refused
        rather than trusted: unverifiable attachment is the same as no attachment.
        """
        search = self._registry_search(emitter)
        if search is None:
            raise MutationClientConfigurationError(
                "the client's event emitter exposes no searchable registry, so "
                "attachment cannot be verified; arming is refused because an "
                "unverifiable witness is indistinguishable from no witness"
            )
        expected = {
            self._on_params: f"provide-client-params.{service_id}.{operation}",
            self._on_before_send: f"before-send.{service_id}.{operation}",
            self._on_needs_retry: f"needs-retry.{service_id}.{operation}",
        }
        for handler, event_name in expected.items():
            if handler not in search(event_name):
                raise MutationClientConfigurationError(
                    f"listener for {event_name} is not reachable in the client's "
                    "event registry; arming is refused because it would record "
                    "nothing while appearing to be armed"
                )

    @staticmethod
    def _registry_search(emitter: Any) -> Any:
        """Return the emitter's handler lookup, or ``None`` if it has none.

        ``client.meta.events`` is an ``EventAliaser`` that forwards to the real
        ``HierarchicalEmitter``; the trie of registered handlers lives on the
        inner emitter as ``_handlers`` (``botocore/hooks.py``). Both levels are
        walked because the aliaser is the documented surface and the inner
        emitter is where the registry actually lives.
        """
        candidates = [emitter, getattr(emitter, "_emitter", None)]
        for candidate in candidates:
            if candidate is None:
                continue
            for attribute in ("_handlers", "_emitter_tree"):
                tree = getattr(candidate, attribute, None)
                search = getattr(tree, "prefix_search", None)
                if search is not None:
                    return search
        return None

    def _on_params(self, **kwargs: Any) -> None:
        """Capture the requested target before serialization.

        Kept separate from ``before-send`` because the two events carry
        different information; merging them would lose one or invent the other.
        """
        self._pending_instance_ids = _instance_ids(kwargs.get("params"))

    @property
    def armed(self) -> bool:
        return self._armed

    # -- observation ---------------------------------------------------------

    def _on_before_send(self, request: Any, **kwargs: Any) -> None:
        headers = _headers(request)
        ids = self._pending_instance_ids
        # A retry re-sends without re-entering provide-client-params, so the
        # captured target carries over. Resetting it per attempt would make
        # every attempt after the first look like it carried no target.
        if not ids:
            ids = _instance_ids_from_body(request)
        self.records.append(
            DispatchRecord(
                operation=STOP_INSTANCES_OPERATION,
                invocation_id=_header(headers, "amz-sdk-invocation-id"),
                attempt=_amz_attempt(headers, "attempt"),
                max_attempts=_amz_attempt(headers, "max"),
                instance_ids=ids,
                captured_during="before-send",
            )
        )

    def _on_needs_retry(self, response: Any = None, **kwargs: Any) -> None:
        # Recorded, never honoured. Note carefully: ``needs-retry`` fires on
        # *every* attempt, including successful ones -- it is the SDK asking
        # "should I retry?", not the SDK retrying. Verified against botocore
        # 1.43.101, where a 200 response still emits the event and the retry
        # checker merely answers no. Counting these events as a violation would
        # therefore fail a perfectly correct single dispatch.
        #
        # So this is context, not evidence of a second attempt. The evidence is
        # the number of ``before-send`` events and the ``attempt=N`` header on
        # each. Prevention remains the client's configuration (ADR 0009); this is
        # the audit trail.
        self.retry_questions.append(
            {
                "response": response is not None,
                "parsed": kwargs.get("parsed") is not None,
                "exception": type(kwargs.get("exception")).__name__
                if kwargs.get("exception") is not None
                else None,
            }
        )

    # -- interpretation ------------------------------------------------------

    @property
    def attempt_count(self) -> int:
        return len(self.records)

    @property
    def distinct_invocation_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for record in self.records:
            if record.invocation_id is not None and record.invocation_id not in seen:
                seen.append(record.invocation_id)
        return tuple(seen)

    def violations(self) -> tuple[str, ...]:
        """Every way the observation fails to show exactly one clean dispatch.

        Returned as a list rather than raised so the caller decides what to do;
        the live path treats any entry as an abort.
        """
        problems: list[str] = []
        count = self.attempt_count
        if count == 0:
            problems.append(
                "no StopInstances attempt reached the sender; either the request "
                "was never attempted or the witness was not armed, and those two "
                "cases must not be confused"
            )
        elif count > 1:
            problems.append(
                f"{count} HTTP attempts reached the wire for one handle() call; "
                "the mutation was dispatched more than once"
            )

        if len(self.distinct_invocation_ids) > 1:
            problems.append(
                f"{len(self.distinct_invocation_ids)} distinct SDK invocation ids "
                "were observed, which means more than one API call was started"
            )

        for record in self.records:
            # The definitive per-attempt evidence: botocore stamps each request
            # with the attempt number it is making and the total it permits.
            if record.attempt is not None and record.attempt > 1:
                problems.append(
                    f"a request reached the wire with attempt={record.attempt}, "
                    "so a second attempt was made after the first"
                )
            if record.max_attempts is not None and record.max_attempts != 1:
                problems.append(
                    f"the SDK declared max={record.max_attempts} attempts for this "
                    "request, so a second attempt was permitted by configuration"
                )
            if record.target_count != 1:
                problems.append(
                    f"the witnessed request carried {record.target_count} instance "
                    "ids; the target must be exactly one"
                )
        return tuple(problems)

    def assert_single_dispatch(self) -> DispatchRecord:
        """Raise unless the evidence shows exactly one clean dispatch."""
        problems = self.violations()
        if problems:
            raise SingleDispatchViolation("; ".join(problems))
        return self.records[0]


# -- header helpers ---------------------------------------------------------


def _headers(request: Any) -> Any:
    headers = getattr(request, "headers", None)
    return headers if headers is not None else {}


def _header(headers: Any, name: str) -> str | None:
    """Read a header case-insensitively, decoding bytes.

    botocore's prepared request holds header values as ``bytes``, and the header
    names are not normalized to a single case. Comparing literally would make
    every field silently ``None`` -- which is how a witness ends up reporting
    nothing and looking like it observed nothing.
    """
    try:
        items = headers.items()
    except AttributeError:  # pragma: no cover - defensive
        return None
    wanted = name.lower()
    for key, value in items:
        if not isinstance(key, str) or key.lower() != wanted:
            continue
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:  # pragma: no cover - defensive
                return None
        return value if isinstance(value, str) else None
    return None


def _amz_attempt(headers: Any, field: str) -> int | None:
    """Parse one field out of the ``amz-sdk-request`` header.

    The header looks like ``attempt=1; max=1``. Both numbers are recorded: the
    attempt number orders the observations, and ``max`` is the SDK's own
    declaration of how many attempts it permits.
    """
    raw = _header(headers, "amz-sdk-request")
    if raw is None:
        return None
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key == field:
            try:
                return int(value)
            except ValueError:  # pragma: no cover - defensive
                return None
    return None


def _instance_ids(params: Any) -> tuple[str, ...]:
    if not isinstance(params, dict):
        return ()
    ids = params.get("InstanceIds")
    if isinstance(ids, str):
        return (ids,)
    if isinstance(ids, (list, tuple)):
        return tuple(str(item) for item in ids)
    return ()


def _instance_ids_from_body(request: Any) -> tuple[str, ...]:
    """Recover the target from a serialized request body as a fallback.

    Used only when the parameter event did not fire. EC2's query protocol
    serializes lists positionally (``InstanceId.1``, ``InstanceId.2``), so the
    key ordering itself carries the count -- which is what makes the one-target
    check still work if only the sender is observed.
    """
    body = getattr(request, "body", None)
    if body is None:
        return ()
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            return ()
    if not isinstance(body, str):
        return ()
    if "&" not in body and "=" not in body:
        return ()
    found: list[tuple[int, str]] = []
    for pair in body.split("&"):
        key, _, value = pair.partition("=")
        name, _, index = key.rpartition(".")
        if name == "InstanceId" and index.isdigit():
            found.append((int(index), value))
    return tuple(value for _, value in sorted(found))