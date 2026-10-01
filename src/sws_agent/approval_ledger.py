"""Durable, transactional approval authority with a revision-ordered ledger.

The M12 investigation established that the pre-M12 approval arrangement let
the approval state machine and the durable audit ledger have independent
lifetimes. A grant recorded on disk could become unredeemable the moment the
process restarted, because the authoritative state lived only in a dict that
was thrown away. This module is the fix: it is the production
``interfaces.ApprovalStore`` implementation, and it holds the approval state
that actually decides whether a side effect may happen.

Two logically distinct responsibilities, deliberately separated:

* **Authoritative state** -- the ``ticket`` table. One row per ticket, mutated
  only inside a write transaction, always through a compare-and-swap on
  ``revision``.
* **Append-only evidence** -- the ``ticket_event`` table. One row per
  committed revision, never updated, never deleted, ordered by ``revision``
  rather than by timestamp.

The split matters. State answers "what is true now"; events answer "how did
it get that way". Collapsing them into one mutable row would lose the
history, and deriving state from the events alone would make replay ambiguous
whenever two events share a timestamp -- which the M12 investigation showed
is not a hypothetical, since the audit ledger's own ordering key is a random
uuid.

Storage technology
------------------
SQLite via the standard library ``sqlite3`` module. No dependency is added
and none is needed: ``sqlite3`` ships with CPython, provides real
transactions with rollback, and takes a cross-process write lock that is
strong enough to make the compare-and-swap meaningful across processes.

Deliberately *not* used:

* ``journal_mode=WAL``. WAL needs a shared-memory sidecar and is not
  supported on every filesystem; if it is unavailable it fails at open time,
  which is at least loud, but the default rollback journal plus
  ``synchronous=FULL`` is portable and fully durable for the single-writer,
  rollback-journal workload an approval store actually is.
* An ORM. The statements here are few, and an inlined statement is auditable
  by reading the file.
* An in-memory database. It would defeat the entire purpose of this module.

Transaction discipline
----------------------
Every state-changing operation runs inside ``BEGIN IMMEDIATE``, which
acquires SQLite's write lock up front. Deferred transactions would allow two
writers to both read, then deadlock on upgrade. ``IMMEDIATE`` makes writers
serialize at the database, which is what turns the compare-and-swap from
best-effort into a real precondition.

Every transition then performs::

    UPDATE ticket SET <new state>, revision = expected + 1
     WHERE ticket_id = ? AND revision = expected

and checks that exactly one row changed. Zero rows means another writer
advanced the ticket first, which is a revision conflict: the store raises and
never overwrites. There is no read-modify-write outside the transaction and
no unbounded retry.

The state update and its event insert commit in the same transaction, so the
store can never durably record one without the other.

Ownership
---------
This store makes **no exclusive-ownership claim**. It is the Phase 2
"option A" model: multiple processes may open the same file concurrently,
because correctness comes from the database's write lock rather than from
refusing to open. Two processes cannot both believe they own the store *and*
violate approval atomicity, since a mutation is only ever committed by the
holder of the write lock and only when its compare-and-swap precondition
still holds.

The honest cost of that choice is that a busy store makes a writer wait up to
``busy_timeout_seconds`` and then fail with
``ApprovalStoreUnavailableError``. It never degrades into a silent retry or,
worse, a lost update.
"""

from __future__ import annotations

import enum
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Final
from uuid import uuid4

from .approval import (
    DEFAULT_APPROVAL_EXECUTION_TTL,
    DEFAULT_APPROVAL_TICKET_TTL,
    LEGAL_APPROVAL_TRANSITIONS,
    ApprovalStoreCorruptionError,
    ApprovalStoreUnavailableError,
    DuplicateTicketError,
    InvalidTransitionError,
    RevisionConflictError,
    UnknownTicketError,
)
from .constants import ApprovalStatus, PotentialAction
from .models import ApprovalTicket

APPROVAL_LEDGER_SCHEMA_VERSION: Final[int] = 1
"""Schema version of the durable approval ledger.

Bumped only by an explicit migration. Opening a ledger whose recorded version
differs is a hard failure: silently reading a layout it does not understand
is exactly how a durable store starts lying.
"""

DEFAULT_APPROVAL_BUSY_TIMEOUT_SECONDS: Final[float] = 5.0
"""How long a writer waits for SQLite's write lock before failing loudly."""

_TICKET_COLUMNS: Final[tuple[str, ...]] = (
    "ticket_id",
    "resource_id",
    "action",
    "rationale",
    "status",
    "created_at",
    "decided_at",
    "decided_by",
    "decision_reason",
    "plan_id",
    "consumed",
    "revision",
    "execution_intent_key",
    "evidence_digest",
    "execution_deadline",
)

_REQUIRED_TABLES: Final[tuple[str, ...]] = ("meta", "ticket", "ticket_event")
_EMPTY_SCHEMA_RETRIES: Final[int] = 50
_EMPTY_SCHEMA_RETRY_SECONDS: Final[float] = 0.02

_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticket (
        ticket_id            TEXT PRIMARY KEY,
        resource_id          TEXT NOT NULL,
        action               TEXT NOT NULL,
        rationale            TEXT NOT NULL,
        status               TEXT NOT NULL,
        created_at           TEXT NOT NULL,
        decided_at           TEXT,
        decided_by           TEXT NOT NULL,
        decision_reason      TEXT NOT NULL,
        plan_id              TEXT,
        consumed             INTEGER NOT NULL,
        revision             INTEGER NOT NULL,
        execution_intent_key TEXT,
        evidence_digest      TEXT,
        execution_deadline   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticket_event (
        event_id        TEXT PRIMARY KEY,
        ticket_id       TEXT NOT NULL REFERENCES ticket(ticket_id),
        revision        INTEGER NOT NULL,
        event_type      TEXT NOT NULL,
        occurred_at     TEXT NOT NULL,
        status          TEXT NOT NULL,
        resource_id     TEXT NOT NULL,
        action          TEXT NOT NULL,
        plan_id         TEXT,
decided_by           TEXT NOT NULL,
        decision_reason      TEXT NOT NULL,
        execution_intent_key TEXT,
        evidence_digest      TEXT,
        execution_deadline   TEXT,
        UNIQUE (ticket_id, revision)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ticket_event_by_ticket
        ON ticket_event (ticket_id, revision)
    """,
)
"""Schema DDL, applied one statement at a time.

Deliberately not a single ``executescript`` call: ``executescript`` implicitly
commits any open transaction before it runs, which would silently end the
``BEGIN IMMEDIATE`` that makes schema creation race-free against a second
process opening the same file for the first time.
"""


class TicketEventType(str, enum.Enum):
    """Event kinds in the durable ticket ledger.

    One kind per committed transition, no more. ``ISSUED`` is the revision-0
    creation event; every other kind names the state the transition produced.
    """

    ISSUED = "issued"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"


_STATUS_EVENTS: Final[dict[ApprovalStatus, TicketEventType]] = {
    ApprovalStatus.PENDING: TicketEventType.ISSUED,
    ApprovalStatus.GRANTED: TicketEventType.GRANTED,
    ApprovalStatus.DENIED: TicketEventType.DENIED,
    ApprovalStatus.EXPIRED: TicketEventType.EXPIRED,
    ApprovalStatus.REVOKED: TicketEventType.REVOKED,
    ApprovalStatus.CONSUMED: TicketEventType.CONSUMED,
}

_MISSING_EVENT: Final[dict[ApprovalStatus, TicketEventType]] = {
    status: kind
    for status, kind in _STATUS_EVENTS.items()
    if status is not ApprovalStatus.PENDING
}
if len(_MISSING_EVENT) != len(ApprovalStatus) - 1:  # pragma: no cover
    raise RuntimeError(
        "every ApprovalStatus needs a terminal TicketEventType; missing: "
        f"{sorted(s.value for s in ApprovalStatus if s not in _MISSING_EVENT)}"
    )


class TicketEvent:
    """One immutable row of the durable approval event ledger.

    ``revision`` is the authoritative ordering key. ``occurred_at`` is
    recorded for humans and is explicitly *not* used to order events, because
    two transitions can legitimately share a timestamp and a ledger that
    depends on clock resolution to order them cannot be replayed reliably.

    An event snapshots the state *as it stood at that revision*, including the
    execution deadline, intent key, and evidence digest. Those three are the
    fields a later transition overwrites or blanks, so storing them only on the
    ticket would lose the record of what a grant actually promised once the
    ticket moved on -- which is exactly the moment an audit needs the answer.
    """

    __slots__ = (
        "action",
        "decided_by",
        "decision_reason",
        "event_id",
        "event_type",
        "evidence_digest",
        "execution_deadline",
        "execution_intent_key",
        "occurred_at",
        "plan_id",
        "resource_id",
        "revision",
        "status",
        "ticket_id",
    )

    def __init__(
        self,
        *,
        event_id: str,
        ticket_id: str,
        revision: int,
        event_type: TicketEventType,
        occurred_at: datetime,
        status: ApprovalStatus,
        resource_id: str,
        action: PotentialAction,
        plan_id: str | None,
        decided_by: str,
        decision_reason: str,
        execution_intent_key: str | None = None,
        evidence_digest: str | None = None,
        execution_deadline: datetime | None = None,
    ) -> None:
        self.event_id = event_id
        self.ticket_id = ticket_id
        self.revision = revision
        self.event_type = event_type
        self.occurred_at = occurred_at
        self.status = status
        self.resource_id = resource_id
        self.action = action
        self.plan_id = plan_id
        self.decided_by = decided_by
        self.decision_reason = decision_reason
        self.execution_intent_key = execution_intent_key
        self.evidence_digest = evidence_digest
        self.execution_deadline = execution_deadline

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TicketEvent(ticket_id={self.ticket_id!r}, "
            f"revision={self.revision}, event_type={self.event_type.value!r}, "
            f"status={self.status.value!r})"
        )


class DurableApprovalStore:
    """Transactional, restart-durable ``interfaces.ApprovalStore``.

    The path is required and has no default. This store never silently
    degrades to an in-memory implementation: a caller that wants ephemeral
    approvals injects ``InMemoryApprovalStore`` explicitly and visibly, while
    a caller that constructs this class has asked for durability and gets an
    error rather than a silent downgrade if the file cannot be used.

    On construction the ledger is verified: SQLite's own ``integrity_check``
    must pass, the recorded schema version must match, every stored ticket
    must round-trip through ``ApprovalTicket`` (which re-enforces the
    status/consumed invariant), and the event ledger must be exactly
    ``revision + 1`` contiguous events per ticket. Any failure raises
    ``ApprovalStoreCorruptionError``. The store never reconstructs a
    plausible-looking state from damaged data.

    ``ttl`` and ``execution_ttl`` are *issuance* windows. They decide the
    ``execution_deadline`` stamped on a new ticket at grant time and are never
    used to recompute or override a deadline already stored. Changing a
    window later therefore affects only tickets granted afterwards; an
    existing approval always keeps the deadline it was granted with.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
        id_source: Callable[[], str] | None = None,
        ttl: timedelta = DEFAULT_APPROVAL_TICKET_TTL,
        execution_ttl: timedelta = DEFAULT_APPROVAL_EXECUTION_TTL,
        busy_timeout_seconds: float = DEFAULT_APPROVAL_BUSY_TIMEOUT_SECONDS,
        verify_on_open: bool = True,
    ) -> None:
        self._path = Path(path)
        self._now: Callable[[], datetime] = now or (
            lambda: datetime.now(timezone.utc)
        )
        self._id_source: Callable[[], str] = id_source or (lambda: uuid4().hex)
        self._ttl = ttl
        self._execution_ttl = execution_ttl
        self._busy_timeout = busy_timeout_seconds
        self._verify_on_open = verify_on_open
        self._closed = False
        if self._now().tzinfo is None:
            raise ValueError(
                "approval ledger clock must return timezone-aware datetimes"
            )
        if ttl <= timedelta(0) or execution_ttl <= timedelta(0):
            raise ValueError("approval TTLs must be positive")
        # Snapshot this *before* connecting: sqlite3 creates an empty file on
        # connect, so deciding "is this new?" afterwards would always say no.
        self._preexisting = self._path.exists() and self._path.stat().st_size > 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if verify_on_open:
            self.verify()

    # -- public ApprovalStore surface ------------------------------------

    def create_ticket(
        self,
        resource_id: str,
        action: PotentialAction,
        rationale: str = "",
        ticket_id: str | None = None,
        plan_id: str | None = None,
        execution_intent_key: str | None = None,
        evidence_digest: str | None = None,
    ) -> ApprovalTicket:
        """Issue a PENDING ticket at revision 0, with its ISSUED event.

        An explicit ``ticket_id`` must be unique. A duplicate raises
        ``DuplicateTicketError`` rather than overwriting the existing ticket:
        overwriting would let a caller silently reset a granted approval back
        to pending, or replace a consumed record, which is precisely the class
        of bug the durable authority exists to prevent.
        """
        moment = self._now()
        ticket = ApprovalTicket(
            ticket_id=ticket_id or uuid4().hex,
            resource_id=resource_id,
            action=action,
            rationale=rationale,
            created_at=moment,
            plan_id=plan_id,
            execution_intent_key=execution_intent_key,
            evidence_digest=evidence_digest,
        )
        with self._transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO ticket ("
                    + ", ".join(_TICKET_COLUMNS)
                    + ") VALUES ("
                    + ", ".join("?" * len(_TICKET_COLUMNS))
                    + ")",
                    _ticket_row(ticket),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateTicketError(
                    f"approval ticket '{ticket.ticket_id}' already exists"
                ) from exc
            self._append_event(conn, ticket, TicketEventType.ISSUED)
        return ticket

    def get(self, ticket_id: str) -> ApprovalTicket:
        """Return the authoritative ticket, materializing any due expiry.

        This is a read that may write. A ticket past its applicable deadline is
        flipped to EXPIRED through a real transactional transition that
        advances ``revision`` and emits an EXPIRED event, exactly as Phase 1's
        in-memory store did lazily. Returning a stale GRANTED ticket that the
        caller then honors would reintroduce the defect M12 exists to remove.
        """
        with self._connection() as conn:
            row = self._select(conn, ticket_id)
        if row is None:
            raise UnknownTicketError(f"unknown approval ticket: {ticket_id}")
        ticket = self._row_to_ticket(row)
        return self._record_due_expiry(ticket)

    def grant(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
        expected_revision: int | None = None,
    ) -> ApprovalTicket:
        """PENDING -> GRANTED, stamping the ticket's own execution deadline."""
        return self._transition(
            ticket_id,
            ApprovalStatus.GRANTED,
            decided_by=decided_by,
            reason=reason,
            expected_revision=expected_revision,
        )

    def deny(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
        expected_revision: int | None = None,
    ) -> ApprovalTicket:
        return self._transition(
            ticket_id,
            ApprovalStatus.DENIED,
            decided_by=decided_by,
            reason=reason,
            expected_revision=expected_revision,
        )

    def expire(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
        expected_revision: int | None = None,
    ) -> ApprovalTicket:
        return self._transition(
            ticket_id,
            ApprovalStatus.EXPIRED,
            decided_by=decided_by,
            reason=reason,
            expected_revision=expected_revision,
        )

    def revoke(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
        expected_revision: int | None = None,
    ) -> ApprovalTicket:
        return self._transition(
            ticket_id,
            ApprovalStatus.REVOKED,
            decided_by=decided_by,
            reason=reason,
            expected_revision=expected_revision,
        )

    def consume(
        self,
        ticket_id: str,
        *,
        expected_revision: int | None = None,
    ) -> ApprovalTicket:
        """GRANTED -> CONSUMED, exactly once.

        The terminal status is what makes a second redemption impossible; the
        transition table rejects it before any write is attempted. This store
        deliberately does **not** make consumption atomic with respect to the
        caller's mutation. ``ExecutionCoordinator`` still consumes after
        ``handler.handle()`` and Phase 2 leaves that ordering untouched; it
        is M13's problem to close.
        """
        return self._transition(
            ticket_id,
            ApprovalStatus.CONSUMED,
            decided_by="",
            reason="",
            expected_revision=expected_revision,
        )

    def pending(self) -> list[ApprovalTicket]:
        """Return tickets still awaiting a decision, in issuance order."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM ticket ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
        outstanding: list[ApprovalTicket] = []
        for row in rows:
            ticket = self._record_due_expiry(self._row_to_ticket(row))
            if ticket.status is ApprovalStatus.PENDING:
                outstanding.append(ticket)
        return outstanding

    # -- observability beyond the protocol -------------------------------

    def events(self, ticket_id: str | None = None) -> list[TicketEvent]:
        """Return ledger events in authoritative (revision) order.

        Ordered by ``revision``, never by ``occurred_at``. With ``ticket_id``
        omitted, events come back grouped by ticket and ordered by ticket's
        first event, so a whole-ledger dump is deterministic.
        """
        with self._connection() as conn:
            if ticket_id is None:
                rows = conn.execute(
                    "SELECT e.* FROM ticket_event e JOIN ("
                    "  SELECT ticket_id, MIN(rowid) AS anchor"
                    "  FROM ticket_event GROUP BY ticket_id"
                    ") a ON a.ticket_id = e.ticket_id"
                    " ORDER BY a.anchor ASC, e.revision ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM ticket_event"
                    " WHERE ticket_id = ? ORDER BY revision ASC",
                    (ticket_id,),
                ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def verify(self) -> None:
        """Fail closed unless the ledger is internally consistent.

        Checks, in order: SQLite's own page-level integrity, the recorded
        schema version, that every ticket row validates as an
        ``ApprovalTicket``, that every event row parses, that no event
        references a missing ticket, and that each ticket has exactly
        ``revision + 1`` contiguous events ``0..revision``.

        The last check is what makes "state committed but event missing"
        detectable rather than silently tolerated.
        """
        with self._connection() as conn:
            try:
                page = conn.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.DatabaseError as exc:
                raise ApprovalStoreCorruptionError(
                    f"approval ledger {self._path} failed integrity_check: {exc}"
                ) from exc
            if page is None or str(page[0]).lower() != "ok":
                raise ApprovalStoreCorruptionError(
                    f"approval ledger {self._path} is structurally corrupt: "
                    f"{page[0] if page else 'no result'}"
                )
            self._verify_schema_version(conn)
            tickets: dict[str, ApprovalTicket] = {}
            for row in conn.execute("SELECT * FROM ticket"):
                try:
                    tickets[row["ticket_id"]] = self._row_to_ticket(row)
                except ApprovalStoreCorruptionError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reported, never repaired
                    raise ApprovalStoreCorruptionError(
                        f"approval ledger {self._path} holds an invalid ticket "
                        f"{row['ticket_id']!r}: {exc}"
                    ) from exc
            seen: dict[str, list[int]] = {}
            for row in conn.execute(
                "SELECT * FROM ticket_event ORDER BY ticket_id, revision"
            ):
                try:
                    event = self._row_to_event(row)
                except ApprovalStoreCorruptionError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reported, never repaired
                    raise ApprovalStoreCorruptionError(
                        f"approval ledger {self._path} holds an invalid event "
                        f"{row['event_id']!r}: {exc}"
                    ) from exc
                if event.ticket_id not in tickets:
                    raise ApprovalStoreCorruptionError(
                        f"approval ledger {self._path} has event "
                        f"{event.event_id!r} for unknown ticket "
                        f"{event.ticket_id!r}"
                    )
                seen.setdefault(event.ticket_id, []).append(event.revision)
            for ticket_id, ticket in tickets.items():
                revisions = seen.get(ticket_id, [])
                expected_revisions = list(range(ticket.revision + 1))
                if sorted(revisions) != expected_revisions:
                    raise ApprovalStoreCorruptionError(
                        f"approval ledger {self._path} ticket {ticket_id!r} is "
                        f"at revision {ticket.revision} but carries events "
                        f"{sorted(revisions)}; expected exactly "
                        f"{expected_revisions}"
                    )

    def close(self) -> None:
        """Mark the store unusable. Idempotent.

        Connections are per-operation, so there is nothing to release; the
        flag exists so a use-after-close fails loudly instead of silently
        reopening the file.
        """
        self._closed = True

    @property
    def path(self) -> Path:
        return self._path

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DurableApprovalStore(path={str(self._path)!r})"

    # -- internals -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self._path,
                isolation_level=None,
                timeout=self._busy_timeout,
            )
        except sqlite3.Error as exc:
            raise ApprovalStoreUnavailableError(
                f"could not open approval ledger {self._path}: {exc}"
            ) from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                f"PRAGMA busy_timeout = {int(self._busy_timeout * 1000)}"
            )
            # Durability over throughput: this store is written a handful of
            # times per human decision, and a committed grant must survive a
            # power loss, not merely a process crash.
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.OperationalError as exc:
            # Transient or environmental: locked, busy, read-only, out of
            # space. Worth retrying.
            conn.close()
            raise ApprovalStoreUnavailableError(
                f"could not configure approval ledger {self._path}: {exc}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            # The file exists but is not a usable database -- junk bytes, a
            # truncated header, another format. Retrying cannot help, so this
            # must not masquerade as a transient condition: a caller that
            # retries "unavailable" forever would never surface the problem.
            conn.close()
            raise ApprovalStoreCorruptionError(
                f"approval ledger {self._path} is not a readable SQLite "
                f"database: {exc}"
            ) from exc
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise ApprovalStoreUnavailableError(
                f"approval ledger {self._path} is closed"
            )
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction, rolling back on any failure.

        ``BEGIN IMMEDIATE`` takes SQLite's write lock at the start, so two
        writers serialize here instead of racing and deadlocking on upgrade.
        A raise anywhere inside rolls the whole thing back, which is what
        guarantees the state update and its event land together or not at
        all.
        """
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise ApprovalStoreUnavailableError(
                    f"approval ledger {self._path} is busy: {exc}"
                ) from exc
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - rollback failed too
                    pass
                raise
            try:
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                # A failed COMMIT can leave the transaction open; roll it back
                # so the connection never closes holding a half-applied write.
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover
                    pass
                raise ApprovalStoreUnavailableError(
                    f"could not commit approval ledger {self._path}: {exc}"
                ) from exc

    def _initialize(self) -> None:
        """Create the schema on first use; validate it on every later open.

        A file that already existed must already be a ledger. Silently running
        ``CREATE TABLE IF NOT EXISTS`` against a foreign or damaged file would
        let the store "repair" it into an empty-but-valid approval database,
        quietly discarding whatever was there -- the opposite of failing
        closed. So the create path is only taken for a genuinely new file, and
        an existing one is held to the recorded schema version.
        """
        if self._preexisting:
            self._adopt_existing()
            return
        with self._transaction() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(APPROVAL_LEDGER_SCHEMA_VERSION),),
                )
            elif str(row[0]) != str(APPROVAL_LEDGER_SCHEMA_VERSION):
                raise ApprovalStoreCorruptionError(
                    f"approval ledger {self._path} has schema version "
                    f"{row[0]}, this build understands "
                    f"{APPROVAL_LEDGER_SCHEMA_VERSION}"
                )

    def _adopt_existing(self) -> None:
        """Open an existing ledger, tolerating a concurrent first open.

        Two processes can legitimately race here: one wins the right to create
        the schema while the other arrives just after the file exists but
        before that schema is committed. An empty ``sqlite_master`` is the only
        signal for that state, and it is indistinguishable from "this file is
        not a ledger", so the empty case is retried briefly and then fails
        closed. A file that has *some* table but not ours is reported at once,
        because retrying could never fix it.
        """
        for attempt in range(_EMPTY_SCHEMA_RETRIES):
            with self._connection() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if not tables:
                    if attempt + 1 < _EMPTY_SCHEMA_RETRIES:
                        sleep(_EMPTY_SCHEMA_RETRY_SECONDS)
                        continue
                    raise ApprovalStoreCorruptionError(
                        f"approval ledger {self._path} has no tables; refusing "
                        "to treat a non-ledger file as an approval authority"
                    )
                self._verify_schema_version(conn)
                return

    def _verify_schema_version(self, conn: sqlite3.Connection) -> None:
        present = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = [name for name in _REQUIRED_TABLES if name not in present]
        if missing:
            raise ApprovalStoreCorruptionError(
                f"approval ledger {self._path} has no "
                f"{'table' if len(missing) == 1 else 'tables'} "
                f"{', '.join(missing)}; refusing to treat a foreign file as an "
                "approval authority"
            )
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise ApprovalStoreCorruptionError(
                f"approval ledger {self._path} has no recorded schema version"
            )
        if str(row[0]) != str(APPROVAL_LEDGER_SCHEMA_VERSION):
            raise ApprovalStoreCorruptionError(
                f"approval ledger {self._path} has schema version {row[0]}, "
                f"this build understands {APPROVAL_LEDGER_SCHEMA_VERSION}"
            )

    def _select(
        self, conn: sqlite3.Connection, ticket_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM ticket WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()

    def _transition(
        self,
        ticket_id: str,
        new_status: ApprovalStatus,
        *,
        decided_by: str,
        reason: str,
        expected_revision: int | None,
    ) -> ApprovalTicket:
        """Apply one state transition atomically, or not at all.

        Three ordered gates refuse before anything is written:

        1. expiry, so an approval that has run out of time dies durably first;
        2. the Phase 1 transition table, so this store accepts exactly the same
           set of moves as the in-memory reference implementation;
        3. the caller's ``expected_revision`` precondition.

        Only then does the compare-and-swap run, inside the same transaction
        that writes the event. ``expected`` is the revision observed before
        this transaction began, so the CAS is what catches a writer that won
        the race between that read and this write -- precisely the window the
        single-process in-memory store could not have.
        """
        with self._connection() as conn:
            row = self._select(conn, ticket_id)
        if row is None:
            raise UnknownTicketError(f"unknown approval ticket: {ticket_id}")

        ticket = self._record_due_expiry(self._row_to_ticket(row))
        permitted = LEGAL_APPROVAL_TRANSITIONS[ticket.status]
        if new_status not in permitted:
            allowed = ", ".join(sorted(s.value for s in permitted)) or "none"
            raise InvalidTransitionError(
                f"ticket '{ticket_id}' cannot transition from "
                f"{ticket.status.value} to {new_status.value} "
                f"(legal targets from {ticket.status.value}: {allowed})"
            )
        current = ticket.revision
        expected = current if expected_revision is None else expected_revision
        if expected != current:
            raise RevisionConflictError(
                f"ticket '{ticket_id}' is at revision {current}, but "
                f"revision {expected} was expected; refusing to overwrite "
                "newer approval state"
            )
        updated = self._apply(
            ticket,
            new_status,
            decided_by=decided_by,
            reason=reason,
            moment=self._now(),
        )
        with self._transaction() as conn:
            self._commit_update(conn, updated, expected)
            self._append_event(conn, updated, _MISSING_EVENT[new_status])
        return updated

    def _commit_update(
        self, conn: sqlite3.Connection, updated: ApprovalTicket, expected: int
    ) -> None:
        """Compare-and-swap the row, requiring exactly one changed row.

        Only fields a transition can actually change are written:
        ``ticket_id``, ``resource_id``, ``action``, ``rationale``, and
        ``created_at`` are identity and issuance facts, so leaving them out of
        the SET list makes their immutability in the durable store obvious
        rather than merely true.

        The WHERE clause is the precondition. If another writer advanced the
        ticket past ``expected`` first, no row matches and the change is
        refused -- the store never overwrites newer approval state.
        """
        cursor = conn.execute(
            "UPDATE ticket SET "
            "status = ?, decided_at = ?, decided_by = ?, "
            "decision_reason = ?, consumed = ?, execution_deadline = ?, "
            "revision = ? "
            "WHERE ticket_id = ? AND revision = ?",
            (
                updated.status.value,
                _format_ts(updated.decided_at),
                updated.decided_by,
                updated.decision_reason,
                1 if updated.consumed else 0,
                _format_ts(updated.execution_deadline),
                expected + 1,
                updated.ticket_id,
                expected,
            ),
        )
        if cursor.rowcount != 1:
            raise RevisionConflictError(
                f"ticket '{updated.ticket_id}' changed underneath this "
                f"transition: expected revision {expected} matched no row"
            )

    def _due_reason(self, ticket: ApprovalTicket) -> str | None:
        """Why ``ticket`` has run out of time, or ``None`` if it has not.

        Both windows are measured against the timestamps *stored on the
        ticket*, never against the store's current configuration, so changing
        a default later can never retroactively expire a live approval.
        PENDING is judged against the decision TTL because that is an issuance
        policy; GRANTED is judged against its own persisted
        ``execution_deadline``, which is the value the grant actually promised.
        """
        if ticket.status is ApprovalStatus.PENDING:
            moment = self._now()
            if moment < ticket.created_at:
                # The decision window has not opened yet, so the ticket is not
                # votable. Expiring (rather than erroring) keeps this
                # fail-closed while still tolerating a clock that has jumped
                # backwards -- this store treats ordering as ``revision``,
                # never as clock order. Checking only the upper bound left
                # ``now - created_at`` negative forever, so damage that pushed
                # ``created_at`` into the future kept a dead request votable
                # indefinitely.
                return (
                    "ticket is not yet open for decision: created_at "
                    f"{_format_ts(ticket.created_at)} is later than now "
                    f"{_format_ts(moment)}"
                )
            if moment - ticket.created_at > self._ttl:
                return f"ticket expired after {self._ttl} without a decision"
            return None
        if ticket.status is ApprovalStatus.GRANTED:
            deadline = ticket.execution_deadline
            if deadline is None:
                # ``ApprovalTicket`` already refuses to represent this state, so
                # getting here means the value was bypassed. Fail closed rather
                # than skipping the bound: a deadline-less GRANTED ticket can
                # never lapse and would stay redeemable forever.
                raise ApprovalStoreCorruptionError(
                    f"ticket {ticket.ticket_id!r} is GRANTED but carries no "
                    "execution_deadline, so it could never expire"
                )
            if self._now() > deadline:
                return (
                    "granted approval expired after its execution window at "
                    f"{_format_ts(deadline)}"
                )
        return None

    def _record_due_expiry(self, ticket: ApprovalTicket) -> ApprovalTicket:
        """Commit an expiry for ``ticket`` if one is due, else return it as-is.

        This deliberately runs in its *own* transaction rather than sharing the
        caller's. If it shared one, a transition that is refused immediately
        afterwards -- ``consume()`` on an approval that has just run out of
        time, say -- would roll the expiry back and leave the ticket GRANTED
        with no evidence. The next read would redo the work, so the outcome
        would not be permanently unsafe, but the refusal would leave no
        durable trace of *why* the approval died. Recording the expiry first
        makes the deadline authoritative even for refused callers.
        """
        if self._due_reason(ticket) is None:
            return ticket
        with self._transaction() as conn:
            row = self._select(conn, ticket.ticket_id)
            if row is None:  # pragma: no cover - deleted mid-flight
                raise UnknownTicketError(
                    f"unknown approval ticket: {ticket.ticket_id}"
                )
            current = self._row_to_ticket(row)
            reason = self._due_reason(current)
            if reason is None:
                return current
            expired = self._apply(
                current,
                ApprovalStatus.EXPIRED,
                decided_by="",
                reason=reason,
                moment=self._now(),
            )
            self._commit_update(conn, expired, current.revision)
            self._append_event(conn, expired, TicketEventType.EXPIRED)
            return expired

    def _apply(
        self,
        ticket: ApprovalTicket,
        new_status: ApprovalStatus,
        *,
        decided_by: str,
        reason: str,
        moment: datetime,
    ) -> ApprovalTicket:
        """Build the successor ticket.

        Mirrors the in-memory store exactly, including the Phase 1 rule that
        ``decided_at`` is written once and ``decision_reason`` describes the
        current state. Redeem passes no reason, so a spent approval keeps the
        reason the human gave.
        """
        changes: dict[str, object] = {
            "status": new_status,
            "consumed": new_status is ApprovalStatus.CONSUMED,
            "revision": ticket.revision + 1,
        }
        if ticket.decided_at is None:
            changes["decided_at"] = moment
        if new_status is ApprovalStatus.GRANTED:
            changes["execution_deadline"] = moment + self._execution_ttl
        if decided_by:
            changes["decided_by"] = decided_by
        if reason:
            changes["decision_reason"] = reason
        return ApprovalTicket.model_validate(
            {**ticket.model_dump(), **changes}
        )

    def _append_event(
        self,
        conn: sqlite3.Connection,
        ticket: ApprovalTicket,
        event_type: TicketEventType,
    ) -> TicketEvent:
        event = TicketEvent(
            event_id=self._id_source(),
            ticket_id=ticket.ticket_id,
            revision=ticket.revision,
            event_type=event_type,
            occurred_at=self._now(),
            status=ticket.status,
            resource_id=ticket.resource_id,
            action=ticket.action,
            plan_id=ticket.plan_id,
            decided_by=ticket.decided_by,
            decision_reason=ticket.decision_reason,
            execution_intent_key=ticket.execution_intent_key,
            evidence_digest=ticket.evidence_digest,
            execution_deadline=ticket.execution_deadline,
        )
        try:
            conn.execute(
                "INSERT INTO ticket_event ("
                "event_id, ticket_id, revision, event_type, occurred_at, "
                "status, resource_id, action, plan_id, decided_by, "
                "decision_reason, execution_intent_key, evidence_digest, "
                "execution_deadline) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.ticket_id,
                    event.revision,
                    event.event_type.value,
                    _format_ts(event.occurred_at),
                    event.status.value,
                    event.resource_id,
                    event.action.value,
                    event.plan_id,
                    event.decided_by,
                    event.decision_reason,
                    event.execution_intent_key,
                    event.evidence_digest,
                    _format_ts(event.execution_deadline),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # The UNIQUE (ticket_id, revision) constraint is the last line of
            # defence for "exactly one event per committed revision". It
            # should be unreachable because the transaction serializes
            # writers, and it aborts rather than writing a second event.
            raise ApprovalStoreCorruptionError(
                f"approval ledger {self._path} refused a second event for "
                f"ticket {event.ticket_id!r} revision {event.revision}: {exc}"
            ) from exc
        return event

    # -- row <-> object mapping ------------------------------------------

    def _row_to_ticket(self, row: sqlite3.Row) -> ApprovalTicket:
        return ApprovalTicket.model_validate(
            {
                "ticket_id": row["ticket_id"],
                "resource_id": row["resource_id"],
                "action": row["action"],
                "rationale": row["rationale"],
                "status": row["status"],
                "created_at": _parse_ts(row["created_at"]),
                "decided_at": _parse_ts(row["decided_at"]),
                "decided_by": row["decided_by"],
                "decision_reason": row["decision_reason"],
                "plan_id": row["plan_id"],
                "consumed": bool(row["consumed"]),
                "revision": row["revision"],
                "execution_intent_key": row["execution_intent_key"],
                "evidence_digest": row["evidence_digest"],
                "execution_deadline": _parse_ts(row["execution_deadline"]),
            }
        )

    def _row_to_event(self, row: sqlite3.Row) -> TicketEvent:
        return TicketEvent(
            event_id=row["event_id"],
            ticket_id=row["ticket_id"],
            revision=row["revision"],
            event_type=TicketEventType(row["event_type"]),
            occurred_at=_parse_ts(row["occurred_at"]),
            status=ApprovalStatus(row["status"]),
            resource_id=row["resource_id"],
            action=PotentialAction(row["action"]),
            plan_id=row["plan_id"],
            decided_by=row["decided_by"],
            decision_reason=row["decision_reason"],
            execution_intent_key=row["execution_intent_key"],
            evidence_digest=row["evidence_digest"],
            execution_deadline=_parse_ts(row["execution_deadline"]),
        )


def _ticket_row(ticket: ApprovalTicket) -> tuple[object, ...]:
    return (
        ticket.ticket_id,
        ticket.resource_id,
        ticket.action.value,
        ticket.rationale,
        ticket.status.value,
        _format_ts(ticket.created_at),
        _format_ts(ticket.decided_at),
        ticket.decided_by,
        ticket.decision_reason,
        ticket.plan_id,
        1 if ticket.consumed else 0,
        ticket.revision,
        ticket.execution_intent_key,
        ticket.evidence_digest,
        _format_ts(ticket.execution_deadline),
    )


def _format_ts(value: datetime | None) -> str | None:
    """Render an aware datetime as canonical UTC ISO-8601.

    Normalizing to UTC makes the stored text a single canonical form, so two
    timestamps written from different local offsets still compare correctly
    and the file is unambiguous on any machine. A naive datetime is rejected
    rather than assumed to be UTC: guessing the offset is how an approval
    quietly expires hours early or late.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("refusing to persist a naive datetime")
    return value.astimezone(timezone.utc).isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a stored timestamp, failing closed on naive or malformed text."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApprovalStoreCorruptionError(
            f"approval ledger holds an unparseable timestamp {value!r}: {exc}"
        ) from exc
    if parsed.tzinfo is None:
        raise ApprovalStoreCorruptionError(
            f"approval ledger holds a naive timestamp {value!r}"
        )
    return parsed