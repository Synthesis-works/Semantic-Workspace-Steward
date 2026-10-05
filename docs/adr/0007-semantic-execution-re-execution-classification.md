# ADR 0007: re-execution is decided by a recorded basis, not by the outcome word

- **Status**: Accepted
- **Date**: 2026
- **Scope**: the durable execution ledger's re-execution decision in `execution_ledger.py`, the schema version that carries the distinction, and what `ExecutionCoordinator` is permitted to assert about it while no mutation handler exists

## Context

ADR 0004 ruled that only a recorded `FAILED` permits a re-execution of the same
intent, and gave the reason: `FAILED` "is the only row that positively
establishes the effect did not happen". Everything else was terminal and
blocking.

ADR 0006 found that this rule is wrong in both directions at once, and that
neither error is visible from the outcome vocabulary.

**It is too strict for `NOT_EXECUTED`.** A row recorded `NOT_EXECUTED` never
reached AWS. It is terminal, so under ADR 0004 it blocks forever, which means an
expired role session, a wrong region, or a missing tag permanently burns an
intent that had no external effect to protect. Meanwhile the resource it named
sits there still running and still actionable. The intent is consumed by a
transient local fault, and no amount of re-approval recovers it.

**It is too permissive for terminal post-states.** ADR 0006's settlement table
routes four situations to `FAILED`: `InvalidInstanceID.NotFound`,
`InvalidInstanceState`, and observations of `terminated`, `shutting-down`, or
`pending`. Each is a terminal fact about the target, and each is spelled
`FAILED`, so ADR 0004 permits re-execution forever. Re-running `StopInstances`
against a terminated instance does not stop anything. It produces
`InvalidInstanceID.NotFound` forever. The retry loop the ledger was built to
prevent is the one it currently enables.

The root cause is that `ExecutionOutcome` records *what was observed*, and
`FAILED` was being read as *whether the same thing may be attempted again*.
Those are different questions, and `FAILED` covers four situations whose blast
radii differ by an order of magnitude: a request that never left the process, a
request AWS definitively rejected, a request whose post-state can never be
reached, and a request that was merely slow.

ADR 0006 also established that these classifications do not exist yet. There is
no mutation handler, so nothing produces the evidence that distinguishes the four
cases. The vocabulary has to exist before the handler that fills it in, because
the alternative is shipping the handler and discovering afterwards that the
ledger cannot record what it learned.

## Decision

> **Retryability is recorded as its own fact, derived from evidence about the
> target and the dispatch, and never from the spelling of the outcome.**

### A closed vocabulary of bases

`ReexecutionClass` in `execution_ledger.py` names why repeating an intent is or
is not safe. It is separate from `ExecutionOutcome` and has no members that mean
"it depends", because a basis that does not decide anything is not a basis.

| Basis | Retryable | Meaning |
|---|---|---|
| `NO_EFFECT` | yes | Nothing external happened. |
| `TRANSIENT_REJECTION` | yes | Rejected for a reason a human can fix; no effect occurred. |
| `POST_STATE_NOT_REACHED` | yes | Accepted; desired post-state not reached in time; target still actionable. |
| `TARGET_INVALID` | no | AWS definitively rejected the request for this target. |
| `POST_STATE_UNREACHABLE` | no | Accepted, and the observed post-state can never reach the postcondition. |
| `OUTCOME_UNKNOWN` | no | Whether the mutation took effect was never established. |
| `EFFECT_ACHIEVED` | no | The intent was achieved; repeating it performs the effect twice. |

Retryability is a set membership (`RETRYABLE_REEXECUTION_CLASSES`), not a flag on
each member. Adding a class is then a deliberate act with an obvious question
attached -- what does this permit? -- instead of a default that silently widens
re-execution.

### The outcome cannot imply the basis

`LEGAL_REEXECUTION_CLASSES_FOR_OUTCOME` is the complete compatibility table, and
it is keyed by every member of `ExecutionOutcome`. Two rows carry the safety:

* `UNKNOWN` admits **only** `OUTCOME_UNKNOWN`. An ambiguous attempt cannot be
  recorded in any form that permits a second execution.
* `FAILED` admits all five of `NO_EFFECT`, `TRANSIENT_REJECTION`,
  `POST_STATE_NOT_REACHED`, `TARGET_INVALID`, and `POST_STATE_UNREACHABLE`.

So the caller must make the distinction `FAILED` previously hid, and the type of
the call they have to write says so. A new outcome cannot be added without being
placed in this table, and being placed means stating which bases it may carry;
the table's keys are asserted equal to `set(ExecutionOutcome)` in the tests.

An incoherent pair is refused at write time, not filtered at read time. Filtering
would let a caller write a false statement into the authority record and merely
be careful about who reads it.

### No default

`record_outcome(..., reexecution_class=...)` is a required keyword argument. A
default of `TRANSIENT_REJECTION` would keep every existing call site working and
every existing meaning wrong: a new caller, or the future mutation handler, would
inherit "FAILED means retryable" without anyone deciding it.

### The ledger reads the basis and only the basis

`reserve` selects a blocking prior with
`state = 'resolved' AND (reexecution_class IS NULL OR reexecution_class NOT IN
(<retryable>))`. The `IS NULL` branch is load-bearing: a settled row with no basis
is treated as blocking, because absence of evidence is not permission. The only
two ways to produce one are a pre-migration file or a corrupted row, and neither
should authorize a second effect.

`_select_reattemptable_prior` selects the lineage reference with the same
predicate. The row a reattempt points at is therefore chosen by the rule that
allowed the reattempt, so permission and lineage cannot disagree.

`verify` treats a terminal row without a basis, an open row with one, and a
terminal row whose basis its own outcome does not admit as corruption. The
column is never trusted without being checked against the outcome.

### Schema version 3, migrated forward

`EXECUTION_LEDGER_SCHEMA_VERSION` moves to 3 with a nullable
`reexecution_class TEXT`. A terminal row requires a non-null basis and an open
row requires null.

The v2 to v3 migration is applied in one transaction, so a file is never left
half-migrated. The backfill is expressed as SQL because it must apply to rows no
caller is reading yet -- the transaction that runs it holds the write lock.

The backfill reconstructs the basis each version 2 row **actually had**, rather
than re-deciding it under the new rules:

| v2 row | v2 permitted a retry? | backfilled basis | now |
|---|---|---|---|
| `resolved` / `failed` | yes | `TRANSIENT_REJECTION` | yes, unchanged |
| `resolved` / `not_executed` | no | `NO_EFFECT` | **yes, widened** |
| `resolved` / `refused` | no | `NO_EFFECT` | **yes, widened** |
| `resolved` / success or partial | no | `EFFECT_ACHIEVED` | no, unchanged |
| `unresolved` / `unknown` | no | `OUTCOME_UNKNOWN` | no, unchanged |
| `reserved`, `attempted` | no | `NULL` | no, unchanged |

Permission widens on exactly the two rows where the evidence of an external
effect was always absent, and on no row where it was present. Reading `failed` as
`TRANSIENT_REJECTION` rather than as `TARGET_INVALID` is deliberate: this file's
rows were judged by version 2's rule, and re-deciding them would rewrite history
in a direction nothing in the old file supports.

The migration re-reads `schema_version` **inside** the write transaction. Both
processes opening a v2 file read `"2"` before either wrote, so both proceed to
`ALTER TABLE ... ADD COLUMN`; the loser of that race would otherwise fail with a
bare driver `duplicate column name`, which is not an error this module's callers
are written to handle, while leaving the file correct. Under the write lock the
second arrival sees version 3 and does nothing, so the migration is exactly-once
per file across processes rather than once per process.

Versions with no migration, in either direction, still fail closed. A file is
migrated only after its tables have proved it is an execution ledger, so a
foreign SQLite file is never rewritten.

### What the coordinator may assert today

> **M15-C superseded this section.** The provisional table below described the
> interim state after M15-B and is retained as the historical record of *why*
> the milestone existed. The coordinator no longer derives a basis from the
> outcome word at all.

There is no mutation handler, so nothing yet distinguishes `TARGET_INVALID` from
`POST_STATE_NOT_REACHABLE`. `ExecutionCoordinator` currently derives:

| recorded outcome | basis | why this is honest |
|---|---|---|
| `UNKNOWN` | `OUTCOME_UNKNOWN` | forced by the compatibility table |
| success, partial | `EFFECT_ACHIEVED` | the postcondition was observed |
| `FAILED`, `REFUSED`, `NOT_EXECUTED` | `NO_EFFECT` | **provisional** |

That last row is the one to watch. It is the conservative direction -- it permits
a retry where a real handler would record `TARGET_INVALID` -- and it is correct
today only because nothing can yet produce the evidence for the stricter basis.
The mapping is marked provisional in `execution.py`. When the mutation handler
lands, `TARGET_INVALID` and `POST_STATE_UNREACHABLE` come from structured
dispatch evidence (an `InvalidInstanceID.NotFound` / `InvalidInstanceState`
rejection, or a terminal post-state), not from this fallback.

## Amendment: M15-C, the evidence that replaces the fallback

M15-C removed the third row. `_reexecution_class_for(outcome)` no longer exists,
and nothing in the codebase derives a `ReexecutionClass` from an
`ExecutionOutcome`.

The fallback was not merely coarse — it was unsafe under the property this ADR
exists to protect, because two of its three rows were reachable from a boundary
that had already dispatched. `FAILED` + `NO_EFFECT` was recorded both for a
definitive API rejection and for a post-dispatch contradiction, and both are
retryable. Neither is safe on its own: a rejection may mean the target does not
exist, and a contradiction after an accepted dispatch may mean the target can
never reach the state. The outcome word carried no information capable of
separating them, so retryability was being granted by omission.

### Three layers, and no layer may skip

| Layer | Type | May it decide re-execution? |
|---|---|---|
| mutation boundary | `DispatchEvidence` | **No.** It has no field for one. |
| coordinator | `classify_dispatch` | Yes, from evidence + a declared contract. |
| ledger | `record_outcome(..., reexecution_class=...)` | No. It refuses illegal pairs. |

`DispatchEvidence` carries a required `DispatchDisposition` — `NOT_DISPATCHED`,
`DISPATCH_REJECTED`, `ACCEPTED`, `DISPATCH_UNKNOWN` (ADR 0006's table, implemented
as written) — plus the provider's `aws_error_code`, `http_status`, and
`exception_class`. It has `extra="forbid"` and no `outcome` or
`reexecution_class` field, so a handler cannot state a retry policy even by
mistake. A `NOT_DISPATCHED` or `ACCEPTED` claim carrying an error code or HTTP
status is rejected at construction: the handler misread its own boundary.

### Where the basis now comes from

`ActionSpec.dispatch_contract` holds two per-action allowlists: rejection codes
and observed post-states, each mapped to a `ReexecutionClass`. No action declares
one. That is the fail-closed default, not an omission — populating the
`StopInstances` tables is part of implementing ADR 0006 and is deliberately not
done here.

| evidence | outcome | basis | reasoning |
|---|---|---|---|
| no evidence, or a raising handler | `UNKNOWN` | `OUTCOME_UNKNOWN` | ignorance is not a failure |
| `DISPATCH_UNKNOWN` | `UNKNOWN` | `OUTCOME_UNKNOWN` | may or may not have been applied |
| `NOT_DISPATCHED` | `NOT_EXECUTED` | `NO_EFFECT` | positive evidence of absence of effect |
| `DISPATCH_REJECTED`, no contract | `UNKNOWN` | `OUTCOME_UNKNOWN` | cannot read an undeclared code |
| `DISPATCH_REJECTED`, code listed | `FAILED` | as declared | e.g. `TARGET_INVALID` |
| `DISPATCH_REJECTED`, unlisted code | `FAILED` | `TRANSIENT_REJECTION` | refused, so nothing was applied |
| `ACCEPTED` + success/partial | success / partial | `EFFECT_ACHIEVED` | postcondition observed |
| `ACCEPTED` + contradiction, state listed | `FAILED` | as declared | `running` ≠ `terminated` |
| `ACCEPTED` + contradiction, unlisted | `UNKNOWN` | `OUTCOME_UNKNOWN` | a guess about a mutation's effect |
| `ACCEPTED` + post-state unestablished | `UNKNOWN` | `OUTCOME_UNKNOWN` | nothing is known about the effect |

**`NO_EFFECT` is reachable from `NOT_DISPATCHED` and from nowhere else.** This is
the milestone's central invariant, asserted exhaustively across the disposition,
verification-status, observed-state, and contract space in
`tests/test_m15c_dispatch_contract.py`. A test also proves the reverse — that
`FAILED` + `NO_EFFECT` cannot be reached for a target that does not exist,
whether the boundary reported a definitive rejection naming it, accepted a
dispatch and then found it terminated, or said nothing at all.

Two consequences worth stating plainly, because both are stricter than M15-B and
both were refused by existing tests that had encoded the looser behaviour:

- A definitive rejection the action cannot classify is now `UNKNOWN`, not a
  retryable `FAILED`. Retrying a rejection the system does not understand is a
  loop, not a correction.
- A post-dispatch contradiction with no declared post-state mapping is now
  `UNKNOWN`, so the replan is **refused** even under a fresh approval. ADR 0003's
  case for permitting a second attempt survives only where the basis is genuinely
  classified; a separately approved retry is not a substitute for evidence.

`NOT_DISPATCHED` also short-circuits verification, which changes the shipped
`NullMutationHandler`: a dry run now records `NOT_EXECUTED` / `NO_EFFECT` instead
of running verification on a mutation that was never sent and observing the
unchanged resource. The structural non-mutation property is now demonstrated in
the ledger rather than inferred from a failed postcondition.

## Frozen invariants

These were ratified at M15-C sign-off and are binding on every future handler,
including the `StopInstances` handler of M15-D. A change to any row is a
change to this ADR, not a local decision inside an implementation.

| Situation | Classification |
|---|---|
| Handler explicitly reports `NOT_DISPATCHED` | `NO_EFFECT` |
| Provider definitively rejects the request | `TRANSIENT_REJECTION`, or a declared terminal rejection |
| Unknown provider rejection code | `TRANSIENT_REJECTION` |
| Request may have crossed the network boundary | `DISPATCH_UNKNOWN` → `UNKNOWN` |
| Accepted + known safe/progressing state | the verifier decides |
| Accepted + undeclared contradictory state | `UNKNOWN` |
| Unknown post-dispatch evidence | `UNKNOWN` |

Two readings are fixed by this table and are the ones most easily lost in a
later refactor.

**`NO_EFFECT` never becomes a synonym for "probably nothing happened."** It is
for an *established* absence of effect. A dispatch that crossed the boundary
without establishing what happened is `OUTCOME_UNKNOWN`, which is terminal.

**`TRANSIENT_REJECTION` means "retry is permitted because the rejection is
established," not "we know this error is transient."** Nothing has observed
that the cause will pass. The word describes what the code may do with a code
no action has classified, not a belief about that code's duration. If a
rejection code turns out to be permanent, the fix is to declare it in
`DispatchContract.rejection_classes` with a terminal basis — never to narrow
the default.

That second reading is why the default is defensible at all. `DISPATCH_REJECTED`
establishes that the provider answered instead of accepting, so nothing was
applied; that is a positive fact, not an absence of one. It is materially
different from `DISPATCH_UNKNOWN`, where even that much is unestablished.
Making every unrecognised rejection terminal would convert an ordinary
throttle into a permanently consumed intent, which is the mirror-image error
and no safer.

By contrast `EndpointConnectionError` *is* `DISPATCH_UNKNOWN`, not
`NOT_DISPATCHED`: a lost response and a pre-send network failure are the same
exception, and the conservative reading is the only one the exception supports.
ADR 0006 records that reversal and its cost.

## Consequences

**The ledger can now be told a true thing it could not be told before.** The
question "may this intent be attempted again?" is answerable from durable state,
and the answer is derived from evidence rather than inferred from a word.

**A dry run leaves the intent re-executable, now for the right reason.** ADR 0005
observed that a dry run records `FAILED` and that this left the intent
re-executable; under ADR 0004 that was an accident of `FAILED` being the retryable
word. It is now stated directly as `NO_EFFECT`.

**`NOT_EXECUTED` and `REFUSED` change behaviour.** Rows that previously blocked a
retry now permit one, and the lineage names the prior execution. This is the
intended correction, and it is the only place the migration widens anything.

**Two terminal safety decisions are enforced rather than described.** Terminal
post-states and definitive rejections are recorded non-retryably, so the retry
loop ADR 0006 identified is closed at the ledger rather than at the handler.

**`UNKNOWN` is unchanged and still costs a human step.** It remains terminal and
blocking; nothing here is a reconciliation mechanism. No automatic UNKNOWN
reconciliation and no stale-reservation takeover were introduced.

**A caller must now know something it did not have to know.** Any new call site
must state its basis, and a future mutation handler must produce evidence that
determines one. That is the intended cost: the decision is where it belongs,
next to the evidence, instead of implied by an outcome label.

**Superseded.** ADR 0004's ruling that "only a recorded `FAILED` permits a
re-execution" is replaced by this ADR on that point alone. ADR 0004's ordering
decision -- consume before effect, reserve to prevent double-claim -- is
unchanged and remains in force. ADR 0006's ledger findings 6 and 9 are resolved
on the ledger side; its handler-side and settle-side findings remain open, because
no handler exists.

## Alternatives considered

**Add a boolean `retryable` column.** Rejected: it collapses the four `FAILED`
situations into the one distinction that does not matter operationally. An
operator reading the ledger could see "not retryable" without learning whether
the resource is gone, whether AWS refused it, or whether nothing ever happened --
three situations requiring three different responses.

**Add new `ExecutionOutcome` members** such as `TARGET_INVALID` or
`TERMINAL_CONTRADICTED`. Rejected: they make the target's state part of the
generic execution vocabulary, so a different action would need its own
parallel set, and the same word would have to be re-derived per action. The basis
is the orthogonal question.

**Keep `FAILED` retryable and add a separate blocklist of terminal reasons.**
Rejected: the blocklist is the same information as the basis with a different
default, and it would leave the common case -- a transient failure nobody
examined -- carrying the unsafe default.

**Make the basis optional, defaulting to the ADR 0004 rule.** Rejected: it keeps
every existing call site silently wrong, which is the failure mode ADR 0006
documented.

**Decide the basis in the ledger from the outcome.** Rejected: the ledger cannot
know whether AWS refused the request or the request never left the process. That
is exactly the evidence the handler sees and the ledger does not.
