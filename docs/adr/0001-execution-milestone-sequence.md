# ADR 0001: The execution milestone sequence (M9 to M13)

- **Status**: Accepted
- **Date**: 2026
- **Scope**: how SWS gets from "machinery exists" to "it may mutate AWS"

## Context

SWS is a read-only inventory and recommendation tool. Its whole value rests on
being able to say what it observed, and being wrong about that is worse than
doing nothing. Acting on infrastructure is a different risk class entirely: a
wrong call stops a running instance, and no amount of correct reasoning
afterwards undoes it.

M9 built the execution machinery — registry, gate, approval binding,
verification, and the `PRE → ATTEMPT → RESULT → POST` ledger contract — while
registering **no** mutation handler, so production could not execute anything.
That was deliberate. The follow-up investigation (M10) then audited M9 for the
places where a *correct-looking* gate could still be fed the wrong evidence, and
found several. Two structural facts follow from those findings.

First, the gate's safety depends entirely on evidence it does not itself
produce. M9's A5 check trusted an observation attached to the request, compared
it against `snapshot.created_at` (the run *start*, not the end of collection),
skipped identity comparisons whenever a field happened to be `None`, never
looked at region, and let the caller pick the facts that "success" would be
measured against. A caller that supplied favourable evidence got a favourable
gate. That is not a bug in one comparison; it is an inverted trust boundary.

Second, M9's durable duplicate detection keyed on `action_plan_id`, which is a
fresh uuid per plan. Re-planning the same intent after a restart produced a new
key, so an ambiguous attempt could be replayed — the exact failure mode the
"never blindly retry" rule was written to prevent.

## Decision

Execution capability is delivered as an ordered sequence. Each milestone is
separately authorized, and no milestone may be merged ahead of its predecessor.

| Milestone | Purpose | May it mutate AWS? |
| --- | --- | --- |
| **M9** | Execution contract, gate, verification, ledger | No handler registered |
| **M10** | Harden the evidence the gate trusts | No handler registered |
| **M11** | Add a **read-only** EC2 observation primitive | No |
| **M12** | Durable approval reconstruction + reconciliation | No |
| **M13** | Register the first real handler (EC2 stop) | Only after M9–M12 |

The ordering is not a convenience; it is the safety argument. The point at
which SWS would first mutate AWS is M13, and by then every input the gate
reasons over is already proven to be independently sourced, fresh, and durable.

### Rules that hold across all five milestones

1. **The caller never supplies safety evidence.** A request may propose; the
   coordinator verifies. Any observation that drives a decision is obtained by
   the coordinator from an injected provider during the gate, and carries
   provider-issued provenance. A request-supplied observation is an explicit
   refusal, not a fallback.
2. **Freshness is bounded on both sides and anchored on collection time.** Not
   older than a maximum age, not from the future, not earlier than the
   snapshot's `collected_at`, and refused outright when the snapshot has no
   `collected_at` to anchor against. A run that cannot establish when its
   inventory was collected cannot establish that its evidence is current.
3. **Identity is mandatory, never optional.** ARN, account, and region must be
   present on both the snapshot record and the observation. A missing fact is
   missing evidence, and missing evidence is a refusal — never a skipped check.
4. **What "success" means is derived from the action, not from the request.** An
   action declares its postcondition; the coordinator verifies that. A caller
   may restate it but not redefine it, and its own expectations are never used
   as the verification target.
5. **Idempotency is durable and deterministic.** Duplicate detection keys on an
   intent derived from stable inputs (snapshot, resource, action) and is
   re-evaluated against the durable ledger on every attempt, so it survives a
   process restart. It is snapshot-scoped by design: a new snapshot
   re-establishes the world.
6. **A provider that fails cannot corrupt the record.** Observation failures are
   contained symmetrically before and after the mutation, and a failure to
   verify yields UNKNOWN — never SUCCESS, and never an exception that escapes
   with the transaction left open.
7. **Unresolved means unresolved.** SWS reports open transactions and their
   intent keys deterministically and read-only. It does not guess, auto-repair,
   or auto-retry an attempt whose outcome is unknown.
8. **No milestone is compressed to "just wire it up."** M11 is read-only on
   purpose: it is the first milestone to touch real AWS, and it is allowed to
   touch it only to *look*.

### Consequences

- The gate grows new refusal reasons, and every one of them is a genuine
  fail-closed path rather than a formality.
- Nothing becomes executable sooner. M13 is the only milestone that can mutate,
  and reaching it requires M9–M12 to have landed.
- Two honest limitations are recorded rather than papered over: durable
  approval reconstruction is deferred to M12 (until then, approval state is
  process-local), and M10's A5 evidence is a provider interface with no
  production implementation until M11.
