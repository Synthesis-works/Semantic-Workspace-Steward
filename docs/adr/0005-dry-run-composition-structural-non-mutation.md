# ADR 0005: One composition root, and a dry run that is structurally incapable of mutating

- **Status**: Accepted
- **Date**: 2026
- **Scope**: how SWS's existing execution parts are assembled into something runnable, what makes that assembly safe to run, and the durability of the audit ledger it writes to

## Context

ADR 0004 fixed the ordering inside `ExecutionCoordinator.execute` and left the milestone in a precise state: the ordering is correct, the approval is claimed before it is spent, the reservation is durable, and **no handler is registered**. That last clause was load-bearing and also entirely conventional. Nothing in the code prevented a future handler from being registered, and nothing had ever run the whole path.

Two gaps followed from that.

**The parts had never been assembled.** `DurableApprovalStore`, `DurableExecutionLedger`, `Ec2InstanceObservationProvider`, and `ExecutionCoordinator` existed and were individually tested, and no code in `src/` constructed them together. Every test that exercised a gated execution supplied its own in-memory stand-in for at least one of them. ADR 0004's ordering was therefore verified in pieces, against doubles, in one process. Whether the four real classes compose correctly against real files was untested — and that composition is exactly what Phase 2's `4/4 workers crossed` measurement was about.

**Non-mutation was a promise, not a property.** "Production registers no handler" describes the current source. It is a statement about a text file, and it has to be restated every time the text changes. Meanwhile `ExecutionMode` has no dry-run member and the coordinator has no dry-run branch: a request that passes the gate calls the injected handler, unconditionally. There was no flag that could make a mutation safe, which is the desirable shape — and also means the *only* thing standing between SWS and a live `StopInstances` call was the absence of an object that does not exist yet.

A third defect was found in the same investigation and is fixed here because the composition depends on it. The audit ledger silently lost records under real concurrency: **169 of 720** across four processes, every child exiting `0`, no malformed lines, no duplicates. The gate's duplicate scans had already been removed on the strength of that measurement (ADR 0004), so the loss was no longer a safety hole — but a ledger that drops evidence without saying so is not a ledger, and the composition below writes to it on every execution.

## Decision

> **The dry run is safe because of what the handler is, not because of a mode it is in. The composition root assembles the real classes over real files and refuses anything that could reach AWS.**

### The composition

`composition.py` provides `build_dry_run_composition`, which constructs `DurableApprovalStore`, `DurableExecutionLedger`, `Ec2InstanceObservationProvider`, and `ExecutionCoordinator` over real files under one directory, and injects a non-mutating handler. Every element is the production class. Nothing in it is a parallel implementation of the workflow, and no test double stands in for a store.

That is the whole point of the milestone. A dry run that exercised a simplified coordinator would prove that the simplified coordinator works.

### Safety is structural

Three properties, each checked at construction rather than asserted in review.

| Property | Enforcement |
| --- | --- |
| The handler cannot reach an environment | `NonMutatingMutationHandler` holds no client, session, credential, or transport. Reaching AWS would require *adding* one, in this module, on purpose. |
| The composition cannot be handed a real handler | `build_dry_run_composition` raises `NonMutatingHandlerRequired` for any handler that is not a `NonMutatingMutationHandler`, before any store is opened. |
| The composition cannot reach AWS even directly | The injected EC2 seam is allow-listed to exactly `describe_instances`. Anything wider is refused by name as `MutatingSeamRequired`. |

The third is an allow-list rather than a deny-list on purpose. A deny-list has to enumerate every mutating EC2 API and is one release behind; an allow-list is exhaustively correct for a capability that is meant to be exactly one read. The check inspects callables only — a seam's data attributes grant no capability, and refusing them would reject an honest test double for the wrong reason.

`RecordingMutationHandler` extends the null handler and records each crossing. The recording is what makes the concurrency assertion mean anything: two workers contending for one intent produce two `execute()` calls, and *the number of recorded crossings* is the claim under test. Two independent operating-system processes are used rather than threads, because the defect being regressed was never visible within one interpreter.

### A dry run records FAILED, and that is the correct result

`STOP_RESOURCE`'s canonical postcondition is `state == "stopped"`. A dry run mutates nothing, the instance is still running, verification is contradicted, and the recorded outcome is `FAILED`.

This is deliberate and it is the honest terminal state for a run that stopped nothing. The alternative — reporting `PARTIALLY_VERIFIED` or succeeding on the grounds that the dry run was *supposed* to do nothing — would mean the system's success criterion had been quietly redefined to match the capability under test. What a dry run demonstrates is that the **coordination** is correct: the gate admitted a properly authorized, freshly observed, action-derived request; exactly one worker claimed it; the approval was spent; the boundary was recorded as crossed; the outcome was derived from evidence rather than assumed. A dry run that reported success would demonstrate less and claim more.

`FAILED` is also the only prior state that ADR 0004 permits a re-execution to follow, so a dry run leaves the intent genuinely re-executable. That is the correct disposition for an attempt that provably had no effect.

### The audit ledger is process-safe

Appends and reads take an exclusive advisory lock on the ledger file, and the in-process view is rebuilt from disk inside that lock. Appends are issued as binary writes against an explicit end-of-file seek rather than as buffered text writes.

Three details are load-bearing:

- **The lock region is one byte at offset 0.** It is never written to; it exists only so the locking primitive has a target. Offset 0 is stable regardless of how large the ledger grows, which an end-of-file region would not be.
- **The duplicate check happens inside the lock, after a refresh.** `record_id` uniqueness was previously checked against a process-local dict, so it could only ever detect a collision this process had itself created. Refreshing first makes the check a statement about the file.
- **`records()` reads through the lock** rather than returning the cache. Returning the cache reported only what *this* process had written, which is the blind spot the original measurement exposed. A second handle, or a second process, now sees the first one's records.

Windows uses `msvcrt.locking` with `LK_NBLCK` retried against an explicit 30-second deadline; POSIX uses `fcntl.flock`. The explicit deadline is not redundant: `msvcrt`'s default retry policy is a fixed internal count, so a contended or wedged writer would otherwise surface as an opaque platform error rather than a named one.

The regression is stated as a measurement, not a vibe: four processes writing 60 records each lost **35** records before the lock existed, with every child still exiting `0`. With the lock, all 240 survive, at several volumes, on both platforms.

### The audit ledger is still not the authority

`JsonlAuditStore` gains no method that could answer "may I proceed?". It cannot reserve, cannot mark an attempt, and cannot record an outcome. Execution state lives in `DurableExecutionLedger` (ADR 0003) and nowhere else. A persistence bug in the evidentiary store cannot become a safety decision, which is the entire reason the two were separated — and it is why making the audit store reliable was necessary but not sufficient for this milestone.

## What this milestone does not do

`STOP_RESOURCE.implemented` stays `False`. No handler that can mutate is registered, anywhere. MCP still exposes exactly nine tools and still has no path to a composition — asserted directly, so that adding one later is a deliberate change rather than an import that slipped in. `ec2:StopInstances` remains unimplemented, and the composition root is not wired into any route.

## Alternatives considered

**Add a `DRY_RUN` member to `ExecutionMode` and branch on it in the coordinator.** Rejected. It puts the safety of a mutation in the same field a caller supplies, so a caller that could set the field could unset it. Safety belongs in an object the caller cannot construct, not in a flag on the request.

**Keep a mutable `has_mutated` / `dry_run` attribute on a single handler that also holds a client.** Rejected, though it is what a `NullMutationHandler` written by someone in a hurry tends to look like. It requires the handler to be correct at every call site rather than incapable at construction, and its safety is undone by any future edit that adds a client attribute.

**Deny-list mutating EC2 methods instead of allow-listing `describe_instances`.** Rejected. A deny-list is behind the moment AWS adds an API, and this capability is small enough to state exhaustively.

**Fix the audit loss with a single-writer queue or a SQLite-backed audit store.** Rejected for now. The lock is a few dozen lines, keeps the format human-readable and append-only, and preserves `CorruptLedgerError` semantics on a malformed line. A store whose durability argument is "we rewrote it in a database" is not obviously better than one whose argument is "the append is serialized", and the schema redesign is a separate decision.

**Have the dry run report `PARTIALLY_VERIFIED` since it performed no effect.** Rejected. It redefines success to match the capability under test, and ADR 0001's rule 4 — that "success" is derived from the action, never from the request — exists precisely to prevent that.

**Assemble the composition inside `mcp/server.py` so it is reachable in production.** Rejected. This milestone's deliverable is a root that *can* be constructed, not a route that constructs it. Wiring it up is the next milestone's decision and should be made against a real mutation handler, with its own authorization.

## Consequences

The gate and both ledgers are now exercised end-to-end against real files in one process and across two, so ADR 0004's ordering has a test that would fail if it regressed. The concurrency claim is stated as a count of boundary crossings rather than as a claim about a return code.

Two honest costs. Execution deduplication is now genuinely cross-process, which means every gated execution takes a SQLite write transaction it previously did not, and the write path is correspondingly slower; the dry run is not free. And the audit ledger's append is now serialized per file, so N concurrent processes appending to one ledger cost roughly N serialized appends — cheap per record, and no longer silently lossy.

A third cost is a limitation rather than a defect. A dry run verifies coordination, not mutation: it cannot demonstrate that `StopInstances` would do the right thing, because it never calls it. The evidence this milestone produces is entirely about SWS's own machinery, and nothing here should be read as evidence about AWS.
