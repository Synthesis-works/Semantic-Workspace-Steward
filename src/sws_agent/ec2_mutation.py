"""The ``ec2:StopInstances`` mutation handler (M15-D, ADR 0008).

M15-D supplies the write half of the boundary that M10's ``MutationHandler``
protocol defines. It adds no MCP tool and no execution path, and
``PotentialAction.STOP_RESOURCE`` remains ``implemented=False`` -- the handler
exists and is tested against a fake client, but flipping ``implemented`` is
gated on independent review of this code.

**What this handler answers is one question:** did the request cross the
dispatch boundary, and what evidence is there about that crossing? It dispatches
exactly once and returns ``DispatchEvidence``. It does not settle, does not
poll, does not decide an outcome, and has no field in which to express a retry
policy -- ``DispatchEvidence`` has no ``outcome`` or ``reexecution_class``
attribute and ``extra="forbid"`` rejects one.

The waiting is somebody else's job. A stop is asynchronous: ``StopInstances``
returns 200 while the instance is ``stopping``, so the request completing and the
intent succeeding are different events. ``ExecutionCoordinator._settle`` waits for
the second one through the injected ``ObservationProvider``. Keeping the two apart
is what stops ``handle()`` from becoming a fifteen-minute call whose return value
describes a process rather than a crossing, and it keeps observation in exactly
one place.

Boundaries, following ``ec2_observation``:

- This module never imports ``boto3`` or ``botocore`` and never constructs a
  client. The client seam is injected. Consequently botocore exceptions are
  classified by **structured class name**, which is what ADR 0006 specifies in any
  case: no message text is parsed, and a ``ClientError``'s code and HTTP status
  come from its own ``response`` rather than from a string comparison.
- No retry loop, and no waiter. See the client precondition below.
- The handler is not constructed by the MCP backend and is not reachable from any
  tool.

The retry precondition
----------------------

botocore's standard retry mode retries ``RequestTimeout``, ``PriorRequestNotComplete``,
HTTP 500/502/503/504, ``ConnectionError``, and throttling codes. Handed a
mutation, that turns one ``handle()`` call into an unknown number of requests on
the wire while SWS records exactly one crossing -- and by ADR 0006 fact 4 nothing
could detect the difference, because ``StopInstancesRequest`` has no
``ClientToken`` to collapse a repeat into one.

So "at most one dispatch" cannot be a comment. The handler **refuses** a client
whose configuration permits a retry: :meth:`StopInstancesHandler.__init__` reads
the client's own botocore config and raises if a second attempt is possible. The
number of bytes that reached AWS and the number of dispatches SWS recorded stay
the same fact, because a misconfigured client is refused rather than trusted.

The two botocore keys are not interchangeable. ``total_max_attempts`` counts
total attempts including the first, so ``1`` disables retries. ``max_attempts``
counts retries *after* the first, and botocore normalizes it as
``total_max_attempts = max_attempts + 1`` (``args.py:600-621``); ``1`` there is
one retry. The handler therefore accepts ``total_max_attempts=1`` and
``max_attempts=0``, and refuses ``max_attempts=1``. An absent or empty ``retries``
mapping is refused as well, because botocore then substitutes
``DEFAULT_MAX_ATTEMPTS`` (3) -- two silent retries. This was verified against
botocore 1.43.101 rather than inferred from the documented key names.
"""

from __future__ import annotations

from typing import Any, Final

from .constants import DispatchDisposition, PotentialAction, SWSResourceType
from .models import DispatchEvidence, ExecutionRequest

# botocore exception class names, matched as strings because this module does not
# import botocore. Each entry is a structured discriminator, not a message probe.
#
# NOT_DISPATCHED is reserved for failures where the *absence of effect is
# established*: the client could not have produced a valid request at all. Every
# one of these is raised before a request is written -- parameter validation,
# credential resolution, region resolution, endpoint resolution, TLS or proxy
# handshake.
_ESTABLISHED_NOT_DISPATCHED: Final[frozenset[str]] = frozenset(
    {
        "ParamValidationError",
        "UnknownServiceError",
        "NoCredentialsError",
        "PartialCredentialsError",
        "CredentialRetrievalError",
        "NoRegionError",
        "UnknownRegionError",
        "InvalidRegionError",
        "EndpointResolutionError",
        "UnknownEndpointError",
        "BaseEndpointResolverError",
        "UnknownSignatureVersionError",
        "SSLError",
        "ProxyConnectionError",
        "InvalidProxiesConfigError",
    }
)

# ADR 0006 as amended at M15-D sign-off. A connect failure is NOT one of these:
# a lost response and a pre-send network failure are the same exception, and
# botocore exposes no signal to separate them, so the disposition is
# DISPATCH_UNKNOWN rather than a manufactured NOT_DISPATCHED.
_DISPATCH_UNKNOWN_EXCEPTIONS: Final[frozenset[str]] = frozenset(
    {
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        "IncompleteReadError",
        "ResponseStreamingError",
        "ConnectionError",
        "HTTPClientError",
    }
)

# Pinned request parameters. Sent explicitly rather than defaulted, because each
# changes the meaning of the call and not merely its speed.
#
# Force:  without it botocore omits the parameter and a future SDK release could
#         change what "stop" does. Force=False refuses to stop an instance with
#         pending instance-store tasks or one mid-transition -- the safe reading.
# Hibernate: False because hibernation preserves the instance's state rather than
#         stopping it, which is a different action under a different intent.
# SkipOsShutdown: False so the OS gets its shutdown signal. True would freeze the
#         filesystem instead, which is not a stop.
FORCE_STOP: Final[bool] = False
HIBERNATE: Final[bool] = False
SKIP_OS_SHUTDOWN: Final[bool] = False

MUTATION_OPERATION: Final[str] = "StopInstances"


class MutationClientConfigurationError(Exception):
    """The injected client is not safe for a mutation (ADR 0008).

    Raised at construction, before any request exists, so a misconfigured client
    can never produce a dispatch. This is deliberately fatal rather than a
    downgrade to a best-effort attempt: the alternative is a client that may
    silently put several mutations on the wire for one recorded crossing.
    """


class StopInstancesHandler:
    """Dispatches one ``ec2:StopInstances`` call and reports what happened.

    Constructions is refused unless three things hold, and each is checked before
    a request exists:

    * the injected client cannot retry (a second attempt is impossible);
    * the region is bound explicitly, so a misconfigured endpoint is visible;
    * the handler is bound to ``STOP_RESOURCE`` alone, so it cannot be wired into
      another action's execution.
    """

    def __init__(
        self,
        client: Any,
        *,
        region: str,
        action: PotentialAction = PotentialAction.STOP_RESOURCE,
    ) -> None:
        if action is not PotentialAction.STOP_RESOURCE:
            raise MutationClientConfigurationError(
                f"StopInstancesHandler may only ever serve STOP_RESOURCE, not "
                f"{action.value!r}; wiring it elsewhere would apply the wrong "
                f"mutation to another action's intent"
            )
        if not region or not region.strip():
            raise MutationClientConfigurationError(
                "region is bound explicitly and may not be empty; an "
                "unbound region would let the client's default decide which "
                "endpoint a mutation is sent to"
            )
        self._assert_no_retry_capability(client)
        self._client = client
        self._region = region.strip()
        self._dispatch_calls: list[dict[str, Any]] = []

    # -- the precondition ----------------------------------------------------

    @staticmethod
    def _assert_no_retry_capability(client: Any) -> None:
        """Refuse a client that might dispatch the same mutation twice.

        Reads the client's own botocore ``Config`` rather than trusting the
        caller, and rejects anything SWS cannot read.

        Two botocore keys must not be confused (verified against botocore
        1.43.101, ``args.py:600-621``). ``total_max_attempts`` counts *total*
        attempts including the first, so ``1`` means "never retry".
        ``max_attempts`` counts *retries after* the first, and botocore
        normalizes it as ``total_max_attempts = max_attempts + 1``. So
        ``max_attempts=1`` is one retry -- two dispatches -- and is refused
        here; the only legacy value that disables retries is ``0``.

        A client built by botocore always exposes the normalized
        ``total_max_attempts`` key, so in practice the first branch below
        decides. The legacy branch exists for clients SWS did not build, which
        is precisely the case where the unsafe value must not be accepted.

        A client that exposes no readable retry configuration is refused too.
        That is the strict direction on purpose: the alternative is a client whose
        retry behaviour SWS cannot see, which is the exact condition this
        precondition exists to rule out. An empty or absent ``retries`` mapping
        is likewise refused -- botocore substitutes ``DEFAULT_MAX_ATTEMPTS``
        (3), which silently permits two retries.
        """
        config = getattr(getattr(client, "meta", None), "config", None)
        retries = getattr(config, "retries", None)
        if not isinstance(retries, dict):
            raise MutationClientConfigurationError(
                "the injected client exposes no readable retry configuration "
                "(expected client.meta.config.retries); a mutation client whose "
                "retry behaviour SWS cannot read is refused"
            )
        if "total_max_attempts" in retries:
            key = "total_max_attempts"
            # total_max_attempts counts the initial request, so exactly one
            # attempt is the value 1.
            safe_value = 1
        elif "max_attempts" in retries:
            key = "max_attempts"
            # max_attempts counts retries *after* the initial request, so the
            # only retry-free value is 0.
            safe_value = 0
        else:
            raise MutationClientConfigurationError(
                "client.meta.config.retries names neither 'total_max_attempts' "
                "nor 'max_attempts', so SWS cannot establish that a retry is "
                "impossible; note that an absent retry setting means botocore "
                "defaults to 3 attempts"
            )
        try:
            attempts = int(retries[key])  # type: ignore[call-overload]
        except (TypeError, ValueError) as exc:
            raise MutationClientConfigurationError(
                f"client.meta.config.retries[{key!r}] is not an integer: "
                f"{retries[key]!r}"
            ) from exc
        if attempts != safe_value:
            raise MutationClientConfigurationError(
                f"the mutation client may make {attempts} attempt(s) "
                f"({key}={attempts!r}); a mutation must dispatch exactly once, "
                f"so the client must be configured with "
                f"{'total_max_attempts=1' if key == 'total_max_attempts' else 'max_attempts=0'}"
            )

    # -- evidence ------------------------------------------------------------

    def handle(self, request: ExecutionRequest) -> DispatchEvidence:
        """Dispatch exactly one stop, or report precisely why one was not sent.

        Returns ``ACCEPTED`` on a 200. That asserts one thing only: the request
        crossed the boundary and was not refused. It does **not** assert the
        instance stopped, and nothing downstream may read it that way -- the
        settle loop and the verifier between them decide whether the postcondition
        was met.
        """
        self._validate(request)
        params: dict[str, Any] = {
            "InstanceIds": [request.resource_id],
            "Force": FORCE_STOP,
            "Hibernate": HIBERNATE,
            "SkipOsShutdown": SKIP_OS_SHUTDOWN,
        }
        try:
            response = self._client.stop_instances(**params)
        except Exception as exc:  # noqa: BLE001 - every failure becomes evidence
            # The one call, recorded whether or not it succeeded. This is the
            # audit trail's answer to "how many dispatches were attempted", and
            # it exists so a caller can prove the count stayed at one.
            self._dispatch_calls.append(params)
            return _evidence_from_exception(exc)
        self._dispatch_calls.append(params)
        return _evidence_from_response(response)

    def _validate(self, request: ExecutionRequest) -> None:
        """Refuse a request this handler may not act on.

        The single-instance rule is structural -- ``resource_id`` is a string, so
        ``InstanceIds`` has one element by construction -- and this checks the
        parts that are not: that the request is for the bound action and resource
        type, and that the id is a plausible single identifier rather than a
        comma-joined list that would silently stop several instances.
        """
        if request.action is not PotentialAction.STOP_RESOURCE:
            raise MutationClientConfigurationError(
                f"this handler serves STOP_RESOURCE, not {request.action.value!r}"
            )
        resource_id = request.resource_id
        if not resource_id or not resource_id.strip():
            raise MutationClientConfigurationError(
                "resource_id must be a single non-blank instance id"
            )
        if resource_id != resource_id.strip() or "," in resource_id:
            raise MutationClientConfigurationError(
                "resource_id must be exactly one instance id with no surrounding "
                f"whitespace and no comma; got {resource_id!r}. A comma would put "
                "several instances behind one intent key, whose partial "
                "StoppingInstances response could not be interpreted"
            )
        if request.snapshot is not None:
            record = _find_record(request.snapshot, resource_id)
            if record is not None and record.resource_type is not SWSResourceType.EC2_INSTANCE:
                raise MutationClientConfigurationError(
                    f"{resource_id!r} is recorded as {record.resource_type.value}, "
                    "not an EC2 instance"
                )

    @property
    def dispatch_calls(self) -> tuple[dict[str, Any], ...]:
        """Every ``StopInstances`` call this handler issued, in order.

        Exactly one entry per ``handle()`` that reached the boundary, whatever the
        outcome. Exists so a test can assert the count directly rather than
        inferring it from a state transition.
        """
        return tuple(self._dispatch_calls)


def _find_record(snapshot: Any, resource_id: str) -> Any:
    """The snapshot's record for ``resource_id``, or ``None``.

    Returns ``None`` when the snapshot is absent or does not carry the instance.
    The caller treats that as "no contradiction available" rather than as an
    error: the resource-type check is a cheap guard against stopping something
    that is recorded as an S3 bucket, not a gate that a sparse snapshot should
    be able to trip.
    """
    records = getattr(snapshot, "resources", None)
    if not isinstance(records, list):
        return None
    for record in records:
        if getattr(record, "resource_id", None) == resource_id:
            return record
    return None


def _evidence_from_response(response: Any) -> DispatchEvidence:
    """Classify a successful ``StopInstances`` call.

    ``ACCEPTED`` and nothing stronger. The response body is summarised into
    ``sanitized`` for the audit trail but is never interpreted here: which
    instances are stopping, and whether the pinned target is among them, is a
    settlement question the verifier answers from an independent observation.
    """
    return DispatchEvidence(
        disposition=DispatchDisposition.ACCEPTED,
        sanitized={
            "operation": MUTATION_OPERATION,
            "stopping_instances": _stopping_instance_ids(response),
        },
    )


def _evidence_from_exception(exc: Exception) -> DispatchEvidence:
    """Map one botocore exception to a disposition, structurally.

    Order matters. ``ClientError`` is checked first because it is the only case
    that carries a response, and a service answer -- even a 5xx one -- is more
    information than any transport failure provides.
    """
    name = type(exc).__name__

    if name == "ClientError":
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            # A ClientError without a response body is not something this
            # classifier can read, so it is not treated as an established
            # rejection.
            return _unknown(exc, note="ClientError carried no readable response")
        code = _error_code(response)
        status = _http_status(response)
        if code == "DryRunOperation":
            # DryRun applies nothing by design, which is an established absence
            # of effect rather than a service refusal.
            return DispatchEvidence(
                disposition=DispatchDisposition.NOT_DISPATCHED,
                sanitized={"operation": MUTATION_OPERATION, "reason": "dry_run"},
            )
        if status is not None and 400 <= status < 500:
            # AWS received the request, understood it, and refused it. Nothing
            # was applied, which is a positive fact and not an absence of one.
            return DispatchEvidence(
                disposition=DispatchDisposition.DISPATCH_REJECTED,
                aws_error_code=code,
                http_status=status,
                exception_class=name,
                sanitized={"operation": MUTATION_OPERATION},
            )
        # 5xx and anything unrecognised: the service may have acted before
        # failing, so nothing is established.
        return _unknown(exc, aws_error_code=code, http_status=status)

    if name in _ESTABLISHED_NOT_DISPATCHED:
        # Deliberately no exception_class / aws_error_code / http_status: the
        # M15-C coherence rule forbids them on NOT_DISPATCHED, because no request
        # was written and so no response exists to quote. The reason is still
        # recorded -- in `sanitized`, where it is audit material rather than a
        # claim about a response that never arrived.
        return DispatchEvidence(
            disposition=DispatchDisposition.NOT_DISPATCHED,
            sanitized={"operation": MUTATION_OPERATION, "reason": name},
        )

    # Everything else -- including EndpointConnectionError and ConnectTimeoutError
    # -- is DISPATCH_UNKNOWN. A connection failure may have carried a fully
    # processed request whose response was lost, so it is not evidence that
    # nothing happened.
    return _unknown(exc)


def _unknown(
    exc: Exception,
    *,
    aws_error_code: str | None = None,
    http_status: int | None = None,
    note: str = "",
) -> DispatchEvidence:
    return DispatchEvidence(
        disposition=DispatchDisposition.DISPATCH_UNKNOWN,
        aws_error_code=aws_error_code,
        http_status=http_status,
        exception_class=type(exc).__name__,
        sanitized={"operation": MUTATION_OPERATION, "note": note},
    )


def _error_code(response: dict[str, Any]) -> str | None:
    error = response.get("Error")
    if isinstance(error, dict):
        code = error.get("Code")
        if isinstance(code, str) and code:
            return code
    return None


def _http_status(response: dict[str, Any]) -> int | None:
    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, dict):
        status = metadata.get("HTTPStatusCode")
        if isinstance(status, int) and not isinstance(status, bool):
            return status
    return None


def _stopping_instance_ids(response: Any) -> list[str]:
    """The instance ids EC2 reported as stopping, or ``[]`` if unreadable.

    Summarised, never trusted. A missing or oddly-shaped ``StoppingInstances``
    does not change the disposition -- the call was accepted either way -- so this
    never raises and never invents an id.
    """
    if not isinstance(response, dict):
        return []
    entries = response.get("StoppingInstances")
    if not isinstance(entries, list):
        return []
    ids: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            instance_id = entry.get("InstanceId")
            if isinstance(instance_id, str) and instance_id:
                ids.append(instance_id)
    return ids
