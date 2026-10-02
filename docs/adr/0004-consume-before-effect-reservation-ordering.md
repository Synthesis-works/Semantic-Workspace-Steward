# ADR 0004: consume authorization before the irreversible effect, reserve to prevent double-claim

- **Status**: Accepted
- **Date**: 2026
- **Scope**: the ordering of the execution reservation, the approval compare-and-swap, and the mutation handler call in `ExecutionCoordinator.execute`, and the state this ordering makes unreachable and the state it cannot avoid creating

## Context

ADR 0003 established a separate durable authority for "has this execution been
claimed, was it attempted, what is known about what happened". It deliberately did
not wire that authority to anything, so nothing about the existing coordinator had
to change. This ADR is the wiring decision, and it has to be made before any handler
can be registered.

Measured in Phase 2, against the real durable approval store, four independent
processes decided and consumed the same ticket: exactly one won the compare-and-swap,
and all four had already crossed the simulated external-effect boundary. The CAS was
working perfectly. It was simply answering a question one step too late. `consume`
appeared after `handler.handle()` in `execute()`, so between the irreversible call
and the durable authorization, the ticket was still `GRANTED` and a second worker
was free to begin.

A CAS protects the row it guards. Nothing in the row survives the API call, so
ordering is the only thing that can protect the effect.

Two weaker dedup mechanisms also sat in the gate. `_durable_attempt_exists` and
`_durable_intent_exists` scanned the audit ledger on every attempt. ADR 0003
measured that store as process-local and lossy under real concurrency (169 of 720
records dropped, no child reporting failure). Leaving them as safety gates while
adding a real one would mean two contradictory notions of execution state, and the
weaker one would be the one answering "may I proceed?".

## Decision

> **Approval is consumed before the irreversible external effect, while the
> execution reservation prevents concurrent workers from claiming the same
> authorized execution.**

The gated path in `ExecutionCoordinator.execute` becomes, in order:

```
gate (read-only refusals; reads the granted ticket)
  → reserve(intent_key, ticket_id, ticket_revision=ticket.revision, worker_id)
  → consume(ticket_id, expected_revision=reservation.ticket_revision)
  → mark_attempted(worker_id)
  → handler.handle()
  → record_outcome(worker_id, outcome)
```

Four properties of that order are load-bearing.

**The reservation precedes consumption.** Claiming first is what makes the CAS
sufficient. Once one worker holds `(intent_key, ticket_id)`, no other worker can
enter the sequence at all, so there is nothing left for the CAS to arbitrate after
the fact. Consuming first would reintroduce the original race with a narrower
window rather than closing it.

**The revision consumed is the revision reserved.** The coordinator passes
`expected_revision=reservation.ticket_revision`. It does not re-read the ticket and
it does not manufacture a revision; the value that the reservation durably captured
is the value the CAS must match. Re-reading would reintroduce a window in which the
ledger asserts one revision and the approval has moved on, which is precisely the
gap Phase 3A closed. ADR 0003 noted that the ledger cannot validate revision
currency because it holds no approval authority; that check belongs here, and this
is where it happens.

**`mark_attempted` precedes the handler.** A crash between `mark_attempted` and
`handler.handle()` and a crash *during* `handler.handle()` leave the same durable
state, and the conservative reading is the safe one. Recording the crossing
*after* the call would let a mid-dispatch crash leave a `RESERVED` row, and
`RESERVED` means the boundary was not crossed — a claim the ledger could not
support. The `PRE → ATTEMPT → RESULT → POST` audit stages keep their existing
meaning; only the approval CAS moves ahead of them.

**Reservation happens only when a handler exists.** With no handler registered the
coordinator returns `NOT_EXECUTED` before any reservation is written, so the
plan-only path stays completely inert. A reservation means an execution claim, and
with nothing executed there is nothing to claim.

The execution ledger is now the authoritative dedup mechanism. The audit-store scans
are removed from the gate and no longer decide whether execution may proceed; the
audit store retains its evidentiary role. A registered handler without an execution
ledger is refused with `DURABLE_LEDGER_REQUIRED`, the same fail-closed condition and
the same reason code as the existing audit-store requirement, because the reason is
identical: crossing a boundary without a durable record of it is never permitted.

## The pair key is not authority to act

ADR 0003's uniqueness claim was `(intent_key, ticket_id)`, and it is retained. What
changed in Phase 4 is what that claim is taken to mean. It names an *authorization
instance* — one approval applied to one effect — not the effect itself. An operator
who issues a second ticket against the same resource and action has not thereby
authorized a second execution, and the pair key would have accepted it, because the
pair key cannot see it: `execution_intent_key` hashes `(snapshot_id, resource_id,
action)` and no ticket or plan identity enters the hash. A fresh ticket is a fresh
*authorization*, not a fresh *effect*.

So `reserve` decides two things, in one operation: whether this pair is free, and
whether this intent is still executable. The first question is the one ADR 0003
settled. The second is the one that keeps a second ticket from replaying an effect
that already happened.

The ruling is that only a recorded `FAILED` permits a re-execution:

| Prior same-intent execution | Fresh ticket |
| --- | --- |
| `RESERVED` | Refused. |
| `ATTEMPTED` | Refused. |
| `VERIFIED_SUCCESS` | Refused. |
| `PARTIALLY_VERIFIED` | Refused. |
| `UNRESOLVED` / `UNKNOWN` | Refused. |
| `FAILED` | Allowed; records `supersedes_reservation_id`. |
| none | Allowed. |

`FAILED` is the only row that positively establishes the effect did not happen, so it
is the only row that can support repeating it. Each of the other rows is refused for a
*different* reason and the refusal names which one, because an operator's next step
differs between them: a `RESERVED` claim may be a crashed worker, an `ATTEMPTED` row
may mean the boundary was crossed, and an `UNRESOLVED` row means nobody knows.

`PARTIALLY_VERIFIED` deserves its own line. "Part of it worked" is not "it failed",
and re-running a partial effect is how a half-applied change becomes a doubled one.
The only safe follow-up is one that examines what landed first, which is
reconciliation, not a retry.

`UNKNOWN` is refused even though it is tempting to treat as retryable: "we do not know
it happened" is not "it did not happen", and repeating an effect against an unknown
outcome is how one unknown becomes two. Overriding this needs an explicit
reconciliation mechanism with its own authorization and audit semantics. A new ticket
is not that mechanism and does not pretend to be.

**The decision is inside the same transaction as the insert.** Reading the prior state
in a separate call before writing would restore exactly the read-then-act race this
seam exists to close — the same shape as the Phase 2 measurement, where four
processes all read "authorized" and all four crossed. The read and the write share one
`BEGIN IMMEDIATE`. A partial unique index cannot express this rule either, because
which rows conflict depends on a recorded outcome, not on the row being inserted.

Atomicity is testable through lineage, and that is how it is tested. When several
`FAILED` attempts exist, the new reservation names the one selected inside its own
transaction — the most recent — not merely "a failure for this intent". The stronger
claim is the observable one: eight processes re-attempting the same failed intent are
*all* permitted by the intent guard, so nothing but the ledger decides the winner, and
afterwards exactly one row may reference the failure. If the selection and the insert
were separate steps, all eight would read the same failed row and eight reservations
would claim it authorized eight separate executions.

A permitted re-execution records `supersedes_reservation_id`, naming the failure it
follows. Two rows sharing an intent key would otherwise be indistinguishable from a
duplicate, and nothing in the file would record why the second was allowed. The
reference is derived by the ledger from durable state — `reserve` takes no parameter
naming the prior execution — so a caller cannot point a reservation at an arbitrary
row, and the chain reads forwards: each attempt names its immediate predecessor. So
`A FAILED → B supersedes A → B FAILED → C supersedes B` is valid and verified, while
`A SUCCESS → B supersedes A` is rejected as corruption, because only a `FAILED`
execution may be superseded. `verify_lineage` proves the stored relations afterwards
are self-consistent, because a durable authority that cannot prove its own contents
is not an authority.

## What the protocol promises

`ExecutionLedger` declares the operations the abstraction actually commits to, rather
than mirroring whatever the SQLite class happens to expose.

`verify()` and `verify_lineage()` are on it because a ledger is expected to apply them
to itself — `verify()` runs on open by default — so an implementation that cannot
prove its own contents cannot honour the contract at all.

The two are separate methods on purpose, and only `verify()` runs at open. `verify()`
establishes that the store is structurally sound and internally consistent;
`verify_lineage()` establishes a different and more expensive claim — that every
supersession reference is well formed — which costs a full row scan plus a chain
walk per row. Folding the second into the first would give every open an O(rows)
semantic proof, blur what "open succeeded" means, and make the more expensive
failure mode part of the cheapest gate. Lineage belongs to a scheduled integrity
sweep, and callers that want it now ask for it explicitly.

`all_executions()` is **not** on it. It has no production caller: the ledger is
enumerated only by tests. Reconciliation, which is what enumeration would exist for, is
deliberately unencoded here, so promoting a test-facing enumerator to a promised
consumer capability would be claiming a surface the system does not use. It stays a
concrete-method detail unless a caller needs it for something other than inspection.

## Schema version 2

`supersedes_reservation_id` makes the ledger schema version 2. A v1 file is rejected
with an explicit version error rather than migrated.

The tempting alternative is to add the column to an existing file in place. That would
mean reading a layout this build does not understand and writing to it, and every
pre-existing row would be silently reinterpreted as having no lineage — which is the
very claim needing proof, not assumption: that no re-execution could have been
authorized under v1. Failing closed keeps the old rows intact for a real migration,
and a migration is a separate decision with its own evidence requirements.

## Where the error taxonomy lives

The execution-ledger exceptions are defined in `interfaces.py`, not in the SQLite
implementation, and re-exported from `execution_ledger.py` as the same class
objects. `execution.py` imports them from the interface layer.

The coordinator has to tell three refusals apart, because each one calls for a
different operator response:

| Condition | Meaning |
| --- | --- |
| `ReservationConflictError` | This exact authorization instance is already claimed. |
| `IntentAlreadyExecutedError` | The same external effect is already protected or known from another execution. |
| `TicketRevisionConflictError` | The authorization changed between reservation and consumption. |

The third is a binding failure, not an execution duplicate: `reserve` captured
revision `R`, and `consume(expected_revision=R)` found `R+1`. Reporting that as a
duplicate would send an operator looking for a second worker when the real cause
is a ticket that moved underneath the reservation. It stays its own refusal
reason, and it leaves the orphaned `RESERVED` row described above rather than
recovering from it.

Collapsing any of these into a generic error and matching on message text was
rejected. The distinction is the whole point of the type: the coordinator is not
a SQLite consumer, and if it has to parse prose to decide whether the world may
have changed, the abstraction has been given back to the message author.

`ExecutionLedgerCorruptionError` is deliberately **not** a
`ReservationConflictError`. A conflict is an ordinary answer to "may I execute
this?"; corruption means the ledger cannot answer that question at all. Were it
a conflict, the coordinator would report unverifiable durable state as ordinary
contention and invite a retry — trusting a record that was just proven
untrustworthy. Corruption propagates and fails closed instead.

## The state this ordering cannot avoid

Two of the four states are unreachable. Before this ADR, an execution could be
attempted while its approval was still `GRANTED`, and could be attempted twice.
Neither is possible now.

One state is created deliberately:

```
CONSUMED approval
+ RESERVED execution
+ no ATTEMPTED record
= authorization spent, external effect not known to have occurred
```

This is the window between `consume` and `mark_attempted`. It exists because
approval consumption is a single durable compare-and-swap and there is no two-phase
commit spanning two independent stores. Closing it would require a transactional
store spanning both, which would undo the separation ADR 0003 argued for.

**This state is never automatically retried and never automatically released.** A
worker that crashed after consuming but before dispatching and one that crashed
inside the handler leave *identical* durable state, so no age threshold, no
heartbeat, and no lease can distinguish them. Releasing on a guess would risk
replaying an effect that already happened, which is worse than a stuck execution
that a human resolves. `is_stale` remains reporting-only.

The symmetric window also exists: a reservation written, then `consume` refused
because the ticket moved. That leaves an orphaned `RESERVED` row and no spent
authorization. It is refused with `TICKET_REVISION_MISMATCH` and left in place for
the same reason — the ledger records what happened and does not clean up after it.

## State vocabulary

The four states mean distinct things, and collapsing any pair destroys a property
the safety model depends on:

| State | Meaning |
| --- | --- |
| `RESERVED` | Claimed. No evidence the external boundary was crossed. |
| `ATTEMPTED` | The boundary was, or may have been, crossed. Outcome open. |
| `RESOLVED` | A definite outcome was recorded. Terminal. |
| `UNRESOLVED` | The outcome cannot be established. Terminal, never auto-retried. |

`RESERVED` is deliberately *not* described as "open" in any sense that implies the
boundary may have been crossed. `may_have_crossed_boundary` is true for every
non-`RESERVED` state, which is why reaching a terminal state requires passing
through `ATTEMPTED`, and why the spend-above window above is precisely the state
where the answer is genuinely unknown rather than known-negative.

## Worker identity

`worker_id` is injected, defaulting to a fresh identity generated **once at
coordinator construction**, never per request. Ownership that changes between the
reservation and the transition proves nothing: the point is that the same worker
that claimed the execution is the only one that may cross the boundary for it. The
default is a generated identifier rather than a hostname or PID, because ownership
uniqueness is what is being enforced and a colliding hostname is a real failure on
a shared host.

## Consequences

Execution deduplication becomes cross-process for the first time, backed by a
database-level `UNIQUE` constraint rather than an in-memory dict that cannot see
another process. `ExecutionResult` gains `reservation_id` so a result points at the
durable claim that authorised it.

The intent-level guard makes the durable record larger and the write path slower: every
`reserve` now reads prior same-intent rows inside its transaction, and a failed
execution may be followed by any number of re-attempt rows for one intent. That is the
cost of being able to prove *why* a second execution was permitted. It also means the
intent guard, not the pair constraint, is what now decides most refusals, and the two
are reported differently: a pair conflict means an authorization instance is claimed,
an intent conflict means the thing that instance would act on may already have been
acted on.

A cost that is specifically *not* accepted: the guard cannot tell a re-execution that
an operator intended from one that a retry loop started, because both arrive as a fresh
ticket. The ledger permits only the second kind's precondition (a prior `FAILED`) and
records the fact, but attributing intent is not something a durable row can do.

Two honest costs are recorded rather than papered over. First, the ledger
accumulates permanently unresolved and stale rows, and something eventually has to
reconcile them; that reconciliation is an operator decision this ADR deliberately
does not encode. Second, the spend-above window means an execution can be stuck
with its authorization already consumed, and un-sticking it is a deliberate
ledger change rather than a retry.

Nothing about this ADR makes anything executable. No handler is registered,
`STOP_RESOURCE.implemented` stays `False`, and MCP exposes no execution tool. The
ordering is fixed while it is still free to be fixed.

## Alternatives considered

**Move only `consume` before the handler, keep the audit scans.** Rejected. It
closes the effect window but leaves two dedup mechanisms disagreeing, and the one
answering "may I proceed" is the one that loses records under concurrency.

**Consume first, reserve second.** Rejected. It narrows the window without closing
it: two workers could both consume-read and only then collide on the reservation,
and the loser would have spent an authorization for an effect it never performed.

**Two-phase approval consumption (`begin_consume` / `commit`).** Rejected for now,
and it is the real fix if the spend-above window ever needs closing. It requires a
second approval-store state and a recovery path for a crash between the two phases
— the same ambiguity, relocated into the approval store. Not a small change, and
not one to make while no handler exists.

**Release a stale `RESERVED` row so a crashed-before-dispatch execution can be
retried.** Rejected on the same indistinguishability as the lease in ADR 0003. A
`RESERVED` row with no `ATTEMPTED` record is ambiguous by construction: it means
either "never dispatched" or "crashed in a window too small to record", and only
one of those may be replayed.