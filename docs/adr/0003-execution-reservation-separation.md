# ADR 0003: execution claims are a durable authority, separate from approval

- **Status**: Accepted
- **Date**: 2026
- **Scope**: where the record of "has this execution already been claimed, was
  it attempted, and what is known about what happened" lives, and why it is not
  the approval ledger and not the audit log

## Context

M13 Phase 2 investigated what stands between an approved action and the AWS
API call it authorises, and measured the gap rather than assuming it. Three
existing mechanisms were supposed to cover it, and all three were found to
leave the same hole.

The approval store's compare-and-swap does protect its own record. Measured
against the real durable approval store, four independent processes decided and
consumed the same ticket and exactly one won. But the process that *lost* the
race had already crossed the simulated external-effect boundary before it
learned it had lost. Four out of four processes performed the effect; one out
of four recorded it. A CAS protects the row it guards, and nothing in the row
exists after the API call returns.

The audit log cannot close the gap either. Its duplicate detection answers from
an in-memory dict populated once when the store was opened, so it is
process-local by construction and can never see another process's records. That
is not hypothetical: with four processes appending genuinely concurrently on
this platform, 169 of 720 records were lost, every child exited successfully,
and not one record was malformed or duplicated. A lost audit record is silent.
Concurrent appends on this platform are not atomic, and `fsync` on the file
handle does not make them so.

Nor can a rejected duplicate be treated as "safe, it must have been me". Two
processes that legitimately hold the same intent after a revocation and
re-approval produce the same key, so a duplicate lookup cannot distinguish
"already done" from "a different authorization instance". A replay defence
built on that key can only ever be either blind or trigger-happy.

So nothing in the system answers the question the execution path actually needs
to ask before calling AWS. This ADR establishes where that answer lives.

## Decision

Execution claims are recorded in a **separate durable store**,
`sws_agent.execution_ledger.DurableExecutionLedger`, exposed as the narrow
`ExecutionLedger` protocol in `sws_agent.interfaces`.

The separation is the point, not a packaging detail. `DurableApprovalStore`
answers *is this action authorized?*; the execution ledger answers *has this
execution already been claimed, was it attempted, and what is known about what
happened?* Folding the second into the first would also break a load-bearing
invariant: `approval_ledger.verify` checks that every ticket carries exactly
`revision + 1` contiguous events, and an execution attempt is not an approval
transition.

A reservation is keyed on **`(intent_key, ticket_id)`**, with a database-level
`UNIQUE` constraint as the real guarantee. The intent key alone is not enough,
because a revoked ticket and its replacement share one; keying on the intent
alone would permanently block a legitimately re-approved attempt. The ticket is
bound to the exact `ticket_revision` it was claimed against, and a mismatch is
reported as `TicketRevisionConflictError` rather than as mere contention,
because it names a different authorization instance.

> **Amended by ADR 0004 ("The pair key is not authority to act").** The claim key
> above is unchanged and the "keying on the intent alone" objection still holds for
> *uniqueness*. What changed is the meaning of the pair: it names an authorization
> instance, not the effect, so it is not by itself authority to act. `reserve` now
> additionally refuses a fresh ticket whenever a prior same-intent execution is
> `RESERVED`, `ATTEMPTED`, `UNRESOLVED`, or resolved with an outcome other than
> `FAILED`. The re-approved attempt this paragraph worried about is not permanently
> blocked — it is permitted once the prior execution records `FAILED`, and that
> supersession is recorded. The intent key became a guard rather than a key.

Claiming is exactly-once and atomic: the read, the absence check, and the
insert share one `BEGIN IMMEDIATE` transaction, so contending processes
serialize at the database. The claim binds a worker, and a losing worker must
not proceed to any external effect.

The binding is enforced on the transitions too, not just on the claim.
`mark_attempted` and `record_outcome` both require the `worker_id` that made
the reservation and refuse anyone else with `ReservationOwnershipError`.
Without that gate the stored `worker_id` would be decorative: any caller that
learned an `(intent_key, ticket_id)` pair could drive another worker's state
machine, and could in particular stamp a `VERIFIED_SUCCESS` onto an execution
it never touched. An exclusive claim whose transitions are not exclusive is
not a claim. Ownership is checked before the revision precondition, because the
two mean different things — one is a lost race worth re-reading, the other is a
caller that must never touch this execution.

The lifecycle is `RESERVED -> ATTEMPTED -> (RESOLVED | UNRESOLVED)`. Reaching a
terminal state requires passing through `ATTEMPTED`, which keeps
`may_have_crossed_boundary` sound when derived from state alone: every
non-`RESERVED` state implies the boundary was recorded as possibly crossed.

Three consequences are deliberately non-negotiable, and they are the ones that
would otherwise be "improved" away later:

1. **A reservation that may have crossed the boundary is never released.**
   `UNRESOLVED` is terminal, has no outgoing edge, and is never automatically
   retried or taken over.
2. **Staleness is reporting-only.** `is_stale` compares `updated_at` against a
   supplied clock and changes nothing. It never frees a claim. There is no age
   threshold that converts a claim into a free slot, because a worker that
   crashed before its external call and one that crashed during it leave
   *identical* durable state; a threshold could not distinguish them, so it
   could only ever release one of them by guesswork.
3. **The ledger holds no approval authority.** It reads `ticket_id` and
   `ticket_revision` as opaque bindings, never consumes an approval, and never
   decides whether an action is authorized.

A consequence of (3) is worth stating plainly, because it is easy to
mis-assume in the consuming phase: for a *new* `(intent_key, ticket_id)` pair
the ledger accepts **any** `ticket_revision`, because it has no way to know
which revision is current without becoming an approval authority. It refuses a
mismatch only against a pair that already exists. A coordinator must therefore
establish revision currency against the approval store itself; the ledger
records the binding but cannot validate it.

The store degrades closed. A damaged page, an unknown schema version, a row
whose state and outcome contradict each other, or a duplicated pair is
`ExecutionLedgerCorruptionError`, never a silently tolerated oddity — including
when it surfaces from a read query, which is where a driver error would
otherwise escape unnoticed.

## Consequences

Exclusivity becomes something the system can demonstrate rather than assume. The
tests contend with eight independent processes over five rounds and require one
winner per round, seven typed refusals, and no duplicated pairs.

`UNKNOWN` is not a retry signal, and no caller may treat it as one. An
`UNRESOLVED` row is a standing question about AWS, and answering it is an
operator decision that this store intentionally does not encode. That is a real
cost: the ledger will accumulate permanently unresolved rows, and something
eventually has to reconcile them.

An operator-driven resolution, and any automatic reconciliation at all, are
future decisions. They are deliberately absent from the transition table, because
a future edge that releases a claim is exactly the kind of change this ADR
exists to make a deliberate one.

## Alternatives considered

**Extend the approval ledger with reservation columns.** Rejected. It conflates
authorization with execution, and execution attempts are not approval
transitions — they would break the contiguous-event invariant that makes a
partially written ticket detectable.

**Rely on the audit log for duplicate detection.** Rejected on measurement. It
is process-local by construction, and it loses records under real concurrency.

**Reserve on the intent key alone.** Rejected. A revoked ticket and its
replacement share one intent key, so this would permanently block a legitimately
re-approved attempt.

**Add a lease, so a stale reservation could be reclaimed automatically.**
Rejected. A worker that crashed before its external call and one that crashed
during it are indistinguishable in durable state, so a lease would reclaim the
second one and could double the external effect. The ambiguity is inherent, not
a gap to be engineered around.

**Use `msvcrt.locking` on the audit file, which lost zero records over five
runs.** Rejected for *this* purpose. It works, but it protects an append-only
evidence log; it is not a transactional store with a uniqueness constraint, and
a lock file adjacent to a log file is not a ledger. The finding is recorded
here because it will be relevant if audit durability is addressed directly.