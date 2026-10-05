# ADR 0011: CloudTrail as independent AWS-side verification

- Status: accepted as **design plus reconciliation logic**. The live stop is
  still **not** authorized by this document.
- Milestone: M15-G (first-live-stop preflight/execution)
- Date: 2026-10-04
- Depends on: ADR 0006 (evidence and settlement), ADR 0008 (handler), ADR 0009
  (production client and scoped IAM), ADR 0010 (single-dispatch witness and
  preflight gate)

## What changed

M15-F built a local witness that observes every `StopInstances` attempt at
botocore's sender, and a preflight gate. Both are good, and neither is
sufficient.

The witness reports **what the dispatching process sent**. That is a
self-report. If the process is wrong, or the observer is defective, or the bytes
took a path the observer cannot see, the witness will report one clean attempt
regardless. Nothing about the witness is independent of the thing it observes.

CloudTrail reports **what AWS received and processed**. It is written by a
different system, it is not reachable from the dispatching process, and it has
different failure modes.

So the milestone's claim had two halves that were previously conflated:
"the instance stopped" and "exactly one request was dispatched". The witness
addressed the second. It could not corroborate itself.

## The ruling this implements

> CloudTrail must be an actual independent verification step, not merely
> evidence collected "if something goes wrong."

And, on precedence:

> If those disagree, the AWS-side evidence wins for determining what actually
> happened, and the run should be treated as unsafe/ambiguous rather than
> trusting the local witness.

That is the design constraint that shapes everything below. An implementation
where the witness is authoritative and CloudTrail is a supporting artifact would
satisfy the letter of M15-F and invert the ruling.

## Decision: `sws_agent.reconciliation`

New module. It consumes CloudTrail evidence as **data** and produces a verdict.
It does not query anything, and it never dispatches.

### Four outcomes, one pass

| Outcome | Meaning |
| --- | --- |
| `CONFIRMED` | Exactly one AWS-side `StopInstances` event for the intended principal, session, target and window, and the witness agrees. |
| `AWS_SIDE_DISAGREEMENT` | AWS-side evidence contradicts the witness. An incident. |
| `UNRESOLVED` | CloudTrail unavailable, errored, or unattrributable. Never a pass. |
| `NO_DISPATCH_OBSERVED` | Both sides agree nothing was dispatched. A verified absence. |

`NO_DISPATCH_OBSERVED` is a fourth outcome rather than folding into
`UNRESOLVED` for an honest reason: when both sides agree nothing happened, the
absence is *corroborated*, and reporting a corroborated fact as an open question
is its own form of dishonesty. It is still not `CONFIRMED` — the claim under
test is that exactly one dispatch occurred, and "nothing happened" does not
satisfy it. It is also the **only** outcome in which a retry is legitimate,
because there is no ambiguity to reconcile.

`CONFIRMED` is the only outcome for which `report.confirmed` is true, and
`require_reconciliation` raises for the other three. A test asserts the full
mapping, because a new outcome added later would otherwise be an unreviewed
pass/fail decision.

### The precedence rule, and why it needed a test

`_check_witness_agreement` compares the witness against `aws_side_count`. The
count is the fact; the witness is the thing under suspicion. When they differ the
finding fails, and the detail string states that the AWS-side record is being
taken as the fact.

The case that matters is: **witness reports one clean attempt, CloudTrail
records two events**. A symmetric implementation would either confirm (trusting
the local process, which is exactly what the ruling forbids) or raise an
unexplained error. It must report `AWS_SIDE_DISAGREEMENT` with
`aws_side_count == 2`. That case is a test, not a comment.

The inverse case is equally important: **one AWS event, two witness attempts**.
That is a genuine violation of no-second-dispatch even though AWS processed only
one — the second attempt was *dispatched*, and the invariant is about dispatch.

And: **one AWS event, two rejected calls** (both with `errorCode`) is two
dispatches. An `errorCode` means AWS did not apply the call, but it was still an
API event. Whether the stop *succeeded* is the settlement's question, answered by
post-state evidence; reconciliation answers only "how many dispatches did AWS
process". Conflating the two would mean a run that failed cleanly four times
read as a success.

### Unavailability is never inferred as absence

`CloudTrailEvidence` keeps `available` separate from `events`.

This distinction is the whole ballgame. A failed query and a query that
legitimately found nothing both present an empty tuple. If they were the same, a
permissions error would silently become "no events", and "no events" would then
have to be read as "probably fine" for the module to be usable during an outage —
which is precisely the downgrade the ruling forbids.

So:

- unavailable, or failed with no reason given → `UNRESOLVED`, `aws_side_count=None`
- available, zero matching events, witness claims ≥1 attempt →
  `AWS_SIDE_DISAGREEMENT`, `aws_side_count=0`

The second is a *contradiction*, not an open question: CloudTrail answered
unambiguously that no such call arrived. Filing that as "unresolved" would let a
real contradiction hide behind an ambiguous label.

### Attribution is four attributes, not one

An event counts as this run's only if **all** of these hold: `eventName ==
StopInstances`, `eventSource == ec2.amazonaws.com`, principal ARN matches, and —
when supplied — the STS session name matches, and the pinned instance is among
its targets, and it falls inside the declared window.

Choices worth stating:

- **Session name is used, not just principal.** A role can dispatch concurrently
  in two sessions; without the session name, one run could confirm itself with
  the other's event.
- **A different principal on the same target is a finding, not noise.** Someone
  else stopping the instance means attribution failed, and that is exactly the
  situation where a naive "filter to my principal and call it clean" would
  produce a false confirmation.
- **Unrelated instances are excluded** from the ambiguity check, so ordinary
  activity elsewhere cannot manufacture doubt.
- **Events for the target outside the window fail the run.** Their existence
  means the window is wrong. Ignoring them would let a confirmation be built on a
  window crafted to exclude the evidence against it.
- **Timezone-naive or inverted windows are refused.** A naive timestamp cannot be
  correlated, and assuming UTC for it would be a guess in the one place a guess
  is least affordable.

### Evidence provenance is checked

`CloudTrailEvidence.reader_principal_arn` must not be the mutating principal.

This is a mechanical expression of a rule the runbook already stated in prose:
the credential that can change things must not be able to inspect itself. A
witness plus a self-read CloudTrail is one actor's account of itself, which is
precisely what the independent cross-check exists to avoid. The reconciliation
refuses such evidence rather than trusting it.

### Correlation is not by SDK invocation id

The witness records `amz-sdk-invocation-id` and M15-F described it as a CloudTrail
join key. That was wrong, and this milestone corrects it.

CloudTrail does not reliably carry the SDK invocation id, so a cross-check
matching on it would find nothing, report "no events", and read as a clean
negative — the worst possible failure for a verification step, because it looks
like a pass.

Correlation is by principal + session + target + window. The invocation id is
retained as a witness-side record, not as a join key.

## CloudTrail is post-dispatch evidence, not a prerequisite

The ruling is explicit that this distinction must be preserved:

> Don't make CloudTrail a prerequisite for dispatch authorization if doing so
> creates a circular dependency. Its role is independent post-dispatch evidence.

It would be circular: CloudTrail cannot confirm a dispatch that has not happened,
so a pre-dispatch gate requiring reconciliation could never pass, and the
pressure to "fix" it would push someone toward weakening the check.

The dependency is therefore prohibited structurally, not merely by convention:
`sws_agent.preflight` must not import `sws_agent.reconciliation`. Two tests
assert this — one from the preflight side, one from the reconciliation side — by
parsing the import graph and the source, so a future refactor cannot reintroduce
the cycle quietly.

The witness-side observation is also read *before* CloudTrail, so its record
exists even if reconciliation fails. But the verdict is never derived from the
witness alone.

## Sequence

```
PRECHECK                 identity, target, policy, shape
      ↓
DRY RUN                  DryRunOperation proves authorization
      ↓
AUTHORIZE SINGLE DISPATCH preflight gate, all findings measured
      ↓
STOP_INSTANCES            exactly one attempt; private/local, no MCP surface
      ↓
LOCAL WITNESS             what this process sent
      ↓
SETTLE / OBSERVE          15s × 40; only `stopped` is success
      ↓
CLOUDTRAIL                what AWS received and processed
      ↓
RECONCILE                 agreement, or an incident
```

Runbook §0.1 carries this sequence, and §12 carries the CloudTrail procedure.

## Two defects found while reviewing, and what they cost

Both were found by reading the module against the ruling rather than by running
it, and both are recorded because the shape of each is instructive.

### A multi-target event confirmed a single dispatch

Scoping an event by `expected in event.instance_ids` is what "this event concerns
our target" means, and it is necessary. It is not sufficient: `StopInstances`
accepts up to a thousand instance ids **in one call**, so a single event can carry
our target and others.

With one such event, the count came out at 1, the principal and session matched,
the window was clean, and the witness agreed on one attempt — so the run
**confirmed**, while a second instance was stopped by the same request. The
runbook's own decision table calls a wrong target a program-level failure, and
the target set must therefore *equal* the pinned id rather than contain it.

Verified by reverting the check: without `target_attribution`, that scenario
returns `confirmed`.

### A stray event authorised a retry

The outcome branches tested `scoped` and `others` but not `stray`. So a
`StopInstances` event for our target **outside** the declared window, paired with
a witness that observed nothing, produced `NO_DISPATCH_OBSERVED` — which is
documented as the one outcome where a retry is legitimate.

That is the worst failure available in this module: an event known to exist,
which could not be placed in time, authorised a second dispatch against an
instance that may already have been stopped. `window_attribution` was reporting
the failure correctly in the rows while the outcome said verified absence, which
is the specific hazard of separating findings from a verdict.

Verified by reverting the guard: without `not stray`, that scenario returns
`no_dispatch_observed` with `retry_permissible=True`.

The fix is one clause in each branch, and the lesson is about structure rather
than the clauses. The retry-licensing rule lived in a docstring while the code
expressed it implicitly through branch structure, so nobody could test it. It is
now `ReconciliationReport.retry_permissible`, true for exactly one outcome, and
a test asserts the full mapping. **A rule with teeth should be a property, not
prose** — prose is what the stray branch contradicted while reading as correct.

## What the module deliberately does not do

- It does not query CloudTrail. Retrieval is a separate step, so retrieval and
  judgement stay separable — a module that did both could quietly retry, widen
  the window, or drop events without that appearing in the verdict.
- It does not dispatch, and imports no AWS SDK transport.
- It does not decide success. Post-state evidence does that (only `stopped`).
- It is not required before dispatch.
- It does not implement the CloudTrail retrieval or IAM for the forensic role;
  those remain runbook steps pending the environment decision from M15-F.

## Exposure

The mutation stays **private and local**. It is not reachable through MCP, and
MCP remains at **nine tools**. Nothing in this milestone changes that, and a
reconciliation module that could be reached from a tool surface would reintroduce
exactly the risk ADR 0009 closed.

## Testing

45 tests in `tests/test_m15g_reconciliation.py`, all hermetic: CloudTrail
evidence is constructed as data; no AWS call, socket, or credential is involved.
One starts from a realistic `LookupEvents` record so the AWS field names are
pinned against something external rather than against how we wish they were —
including that the target appears in both `Resources[].ResourceId` and
`RequestParameters.instanceIdsSet.items[].instanceId`, and that the session is
recovered from `Username`'s `ROLE_ID:session` form.

Three groups are falsification tests for the ruling itself rather than coverage:

1. **AWS wins** — clean witness against two AWS events fails, with the AWS count
   reported as the fact.
2. **Unavailability is not benign** — unavailable CloudTrail is `UNRESOLVED` even
   with a perfect witness, and `available=False` is distinguishable from an empty
   result.
3. **No circularity** — the pre-dispatch gate must not import the reconciler, and
   the reconciler's evidence arguments are required and keyword-only so a caller
   cannot reconcile from nothing or pass evidence positionally by accident.

Two checks in this milestone reused a text scan that was already known to
misfire, and both were rewritten to parse the AST: the preflight module's
"contains no mutation call" test matched `is_stop_instances(self)` as though it
were a call, and the reconciler's version of the same check matched its own
legitimate property. The lesson is recorded rather than absorbed: a text scan of
source is not evidence about behaviour, and three of them in this milestone have
now been wrong.