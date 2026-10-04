"""M14-A: one real composition root, wired to a handler that cannot mutate.

M13 proved the coordinator, the approval ledger, and the execution ledger are
individually correct, and it measured the two places where that was not enough:
four processes crossed the boundary where only one had approval, and 169 of 720
audit records vanished. This module exists because correct components that are
never assembled have never demonstrated anything.

It assembles them -- :class:`DurableApprovalStore`,
:class:`DurableExecutionLedger`, :class:`Ec2InstanceObservationProvider` and
:class:`ExecutionCoordinator` -- against real files, so a dry run exercises the
same gate, the same reservation, the same approval consumption, the same
attempt marker, and the same outcome recording that a live mutation would.

The safety argument is deliberately structural rather than conventional.
``ExecutionMode`` has no dry-run member and the coordinator has no dry-run
branch: a request that passes the gate calls the injected handler,
unconditionally. There is no flag that makes a mutation safe. Safety comes from
*what the handler is*, and this module ships handlers that cannot reach AWS:

* :class:`NonMutatingMutationHandler` holds no client, session, credential, or
  transport. Reaching an environment would require adding one, which is a
  visible edit here rather than an accident.
* :func:`build_dry_run_composition` refuses any handler that is not one of
  those, so the "dry run" composition cannot be quietly handed a real one.
* It refuses an EC2 seam that exposes anything beyond ``describe_instances``,
  so the composition's only AWS capability is reading an instance's state.

Nothing here is wired into the MCP server, and ``mcp.server`` still registers no
handler at all. A future milestone that implements ``ec2:StopInstances`` will
have to change this module on purpose.

A dry run does not, and cannot, verify success: no mutation occurs, so the
canonical postcondition (``state == "stopped"``) is honestly unmet and the
recorded outcome is FAILED. That is the correct terminal state for a run that
stopped nothing. What the dry run demonstrates is that the coordination is
correct, not that the mutation works.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from sws_agent.approval_ledger import DurableApprovalStore
from sws_agent.audit import LEDGER_FILENAME, JsonlAuditStore
from sws_agent.ec2_observation import DEFAULT_PARTITION, Ec2InstanceObservationProvider
from sws_agent.execution import ExecutionCoordinator, MutationHandler
from sws_agent.execution_ledger import DurableExecutionLedger
from sws_agent.models import ExecutionRequest, MutationAttempt

APPROVAL_LEDGER_FILENAME: Final[str] = "approvals.sqlite3"
"""Durable approval ledger file created inside the composition's ledger dir."""

EXECUTION_LEDGER_FILENAME: Final[str] = "executions.sqlite3"
"""Durable execution reservation ledger file inside the composition's dir."""

READ_ONLY_EC2_METHODS: Final[frozenset[str]] = frozenset({"describe_instances"})
"""The only AWS call the dry-run composition is allowed to make.

An allow-list rather than a deny-list on purpose. A deny-list has to enumerate
every mutating API in existence and is one release behind; an allow-list is
exhaustively correct for a capability that is meant to be exactly one read.
"""


class CompositionError(RuntimeError):
    """A composition request was refused before anything was constructed."""


class NonMutatingHandlerRequired(CompositionError):
    """A handler that could reach an environment was offered to a dry run."""


class MutatingSeamRequired(CompositionError):
    """An EC2 seam exposing more than a read was offered to a dry run."""


class NonMutatingMutationHandler:
    """Base for mutation handlers structurally incapable of mutation.

    There is no ``__init__`` and no state: a subclass implements
    :meth:`handle` and returns a result. It cannot perform a side effect
    because it is never given anything through which one could be performed --
    no client, no session, no credential, no transport, no network handle.
    Adding one would mean adding it here, which is why this base class is
    deliberately empty.
    """

    def handle(self, request: ExecutionRequest) -> MutationAttempt:  # pragma: no cover
        raise NotImplementedError


class NullMutationHandler(NonMutatingMutationHandler):
    """Crosses the boundary in name only and reports a definite no-op.

    The attempt is neither ambiguous nor an error: nothing was dispatched and
    nothing failed. Recording it as a definite outcome is what lets the
    coordinator proceed to verification instead of parking the execution in
    ``UNRESOLVED``.
    """

    def handle(self, request: ExecutionRequest) -> MutationAttempt:
        return MutationAttempt(
            ambiguous=False,
            call_error=False,
            sanitized={
                "handler": type(self).__name__,
                "mutating": False,
                "action": request.action.value,
                "resource_id": request.resource_id,
            },
        )


class RecordingMutationHandler(NullMutationHandler):
    """A null handler that keeps every crossing it was handed.

    The recording is what makes the concurrency assertion meaningful: two
    workers contending for one intent produce two ``execute()`` calls and, if
    the boundary is correct, exactly one recorded crossing.
    """

    def __init__(self) -> None:
        self._crossings: list[ExecutionRequest] = []

    def handle(self, request: ExecutionRequest) -> MutationAttempt:
        self._crossings.append(request)
        return super().handle(request)

    def crossings(self) -> tuple[ExecutionRequest, ...]:
        """Every request that reached the boundary, in crossing order."""
        return tuple(self._crossings)

    def __len__(self) -> int:
        return len(self._crossings)


class ReadOnlyEc2Seam:
    """Narrows a wider client to the single read this composition needs.

    Production has an ``AwsMultiClient`` that also lists buckets, functions,
    and costs. Handing that object to the dry-run composition would grant
    capabilities the dry run has no use for, so this wrapper exposes exactly
    one method and forwards it.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def describe_instances(self) -> Any:
        return self._client.describe_instances()


def _assert_read_only_seam(client: Any) -> None:
    """Refuse any seam whose *capability* surface exceeds the permitted read.

    Only callables count. A seam's data attributes -- a cached state, a call
    counter -- grant no capability, and refusing them would make the check
    reject an honest fake for the wrong reason. What must not be present is a
    method that could change something, so that is what is enumerated.

    This makes "the dry run can only describe instances" a checked invariant
    rather than a review promise: a raw boto3 EC2 client, or a multi-client
    facade, is rejected by name before anything is constructed.
    """
    exposed = {
        name
        for name in dir(client)
        if not name.startswith("_") and callable(getattr(client, name, None))
    }
    unexpected = exposed - READ_ONLY_EC2_METHODS
    if unexpected:
        raise MutatingSeamRequired(
            "the dry-run composition accepts only an EC2 seam exposing "
            f"{sorted(READ_ONLY_EC2_METHODS)}, but the supplied seam also "
            f"exposes {sorted(unexpected)}; wrap it in ReadOnlyEc2Seam first"
        )
    if "describe_instances" not in exposed:
        raise MutatingSeamRequired(
            "the supplied seam does not expose describe_instances, so the "
            "composition cannot obtain the pre-attempt observation the gate "
            "requires"
        )


@dataclass(frozen=True)
class DryRunComposition:
    """Everything one dry run needs, plus the handle to release it.

    Frozen because the coordinator's identity -- its ``worker_id`` -- is fixed
    at construction and re-binding the bundle's parts would silently break the
    ownership invariant that ties a reservation to the worker that made it.
    """

    coordinator: ExecutionCoordinator
    approval_store: DurableApprovalStore
    execution_ledger: DurableExecutionLedger
    observer: Ec2InstanceObservationProvider
    handler: NonMutatingMutationHandler
    audit_store: JsonlAuditStore | None

    def close(self) -> None:
        """Release every store this composition opened, best effort.

        Best effort on purpose: a composition torn down after a test failure
        should surface its own error rather than masking it with a close
        error, and every store here fails closed on its own if left open.
        """
        for closeable in (
            self.audit_store,
            self.execution_ledger,
            self.approval_store,
        ):
            if closeable is None:
                continue
            try:
                closeable.close()
            except Exception:  # noqa: BLE001,S110 - teardown must not mask cause
                pass


def build_dry_run_composition(
    *,
    ledger_dir: str | Path,
    ec2_client: Any,
    region: str,
    handler: NonMutatingMutationHandler | None = None,
    partition: str = DEFAULT_PARTITION,
    audit: bool = True,
    now: Callable[[], datetime] | None = None,
    approval_id_source: Callable[[], str] | None = None,
    execution_id_source: Callable[[], str] | None = None,
    audit_id_source: Callable[[], str] | None = None,
    worker_id: str | None = None,
    max_observation_age_seconds: int | None = None,
) -> DryRunComposition:
    """Assemble the real coordinator over real durable stores.

    Every store is a real file under ``ledger_dir``: this is not a parallel
    in-memory implementation of the workflow, it is the production classes
    wired together. ``handler`` defaults to a
    :class:`RecordingMutationHandler` and may only ever be a
    :class:`NonMutatingMutationHandler`.

    ``audit=False`` omits the audit store. The result is a composition that
    refuses every gated execution with ``DURABLE_LEDGER_REQUIRED``: the
    coordinator will not cross a boundary it has no durable record of, and
    that check is deliberately independent of the execution ledger's. The
    parameter exists so that refusal path is constructible and assertable,
    not as a way to execute without evidence.
    """
    _assert_read_only_seam(ec2_client)
    resolved_handler = handler if handler is not None else RecordingMutationHandler()
    if not isinstance(resolved_handler, NonMutatingMutationHandler):
        raise NonMutatingHandlerRequired(
            "a dry-run composition accepts only NonMutatingMutationHandler "
            f"instances, but received {type(resolved_handler).__name__}; "
            "this composition root must not be given a handler that can "
            "reach an environment"
        )

    root = Path(ledger_dir)
    approval_store = DurableApprovalStore(
        root / APPROVAL_LEDGER_FILENAME, now=now, id_source=approval_id_source
    )
    try:
        execution_ledger = DurableExecutionLedger(
            root / EXECUTION_LEDGER_FILENAME,
            now=now,
            id_source=execution_id_source,
        )
        try:
            audit_store = (
                JsonlAuditStore(
                    root / LEDGER_FILENAME, now=now, id_source=audit_id_source
                )
                if audit
                else None
            )
            try:
                observer = Ec2InstanceObservationProvider(
                    ec2_client,
                    region=region,
                    partition=partition,
                    now=now,
                )
                extra: dict[str, Any] = {}
                if max_observation_age_seconds is not None:
                    extra["max_observation_age_seconds"] = (
                        max_observation_age_seconds
                    )
                coordinator = ExecutionCoordinator(
                    approval_store=approval_store,
                    execution_ledger=execution_ledger,
                    handler=cast_mutation_handler(resolved_handler),
                    observer=observer,
                    audit_store=audit_store,
                    worker_id=worker_id,
                    now=now,
                    **extra,
                )
            except BaseException:
                if audit_store is not None:
                    audit_store.close()
                raise
        except BaseException:
            execution_ledger.close()
            raise
    except BaseException:
        approval_store.close()
        raise

    return DryRunComposition(
        coordinator=coordinator,
        approval_store=approval_store,
        execution_ledger=execution_ledger,
        observer=observer,
        handler=resolved_handler,
        audit_store=audit_store,
    )


def cast_mutation_handler(handler: NonMutatingMutationHandler) -> MutationHandler:
    """Narrow the concrete handler to the protocol the coordinator expects.

    ``NonMutatingMutationHandler`` already satisfies ``MutationHandler``; this
    exists so the narrowing is stated in one place instead of being implied by
    an ``isinstance`` check that a reader has to reverse-engineer.
    """
    assert isinstance(handler, MutationHandler)  # noqa: S101 - invariant check
    return handler


def iter_dry_run_ledger_paths(ledger_dir: str | Path) -> Iterator[Path]:
    """Yield the ledger paths a composition owns, in a stable order."""
    root = Path(ledger_dir)
    yield root / APPROVAL_LEDGER_FILENAME
    yield root / EXECUTION_LEDGER_FILENAME
    yield root / LEDGER_FILENAME
