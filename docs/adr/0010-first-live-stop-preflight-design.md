# ADR 0010: First live stop — preflight design and the single-dispatch invariant

- Status: accepted as **design only**. The live stop is **not** authorized by this
  document and remains a separate authorization.
- Milestone: M15-F
- Date: 2026-10-04
- Depends on: ADR 0006 (evidence and settlement), ADR 0008 (handler), ADR 0009
  (production client and scoped IAM), ADR 0005 (dry-run composition)

## The invariant this milestone exists to satisfy

The ruling was explicit:

> The live experiment must prove not merely that the instance stopped, but that
> the entire safety chain prevented an unintended second dispatch.

Those are different claims with different evidence. "The instance stopped" is
confirmed by `DescribeInstances` and is easy to obtain. "No second dispatch
occurred" is a claim about something that must *not* have happened, and a
successful stop is not evidence for it. A run that stopped the instance twice
would also report "the instance stopped."

The rest of M15 is therefore not sufficient on its own. A retried
`StopInstances` produces one stopped instance and two API calls, and every
invariant written so far — handler-level retry refusal, factory-level
`total_max_attempts=1`, ledger crossing counts — is a *prevention* mechanism.
Prevention that is never observed is prevention that is merely believed.

## Why the existing evidence cannot show this

`StopInstancesRequest` has no `ClientToken`, which is exactly why ADR 0006
classified a repeat as unrecoverable: there is no idempotency token to collapse
two calls into one crossing, so SWS's record and the wire can disagree with
nothing to detect it. The M15-E precondition removes the *known* cause of a
retry. It cannot observe an unknown one — a transport that re-sends, an
interposing proxy, a future SDK behavior change.

So M15-F adds an observation channel rather than another prevention mechanism.

## Decision: the dispatch witness

New module `sws_agent.mutation_evidence.DispatchWitness`, behind the same
hermeticity boundary as the production client.

The witness attaches to botocore's own event pipeline and records every
`StopInstances` attempt that reaches the sender. Three independent observations
are collected, and the design point is that they can disagree:

| Signal | Source | What it establishes |
| --- | --- | --- |
| attempt count | `before-send.ec2.StopInstances` | How many HTTP requests were attempted. Fires once per attempt, at the last point before bytes leave (`endpoint.py:277`). |
| `attempt=N` | `amz-sdk-request` header | The SDK's own numbering of which attempt this is. |
| `max=M` | same header | The SDK's own declaration of how many attempts it permits. |
| invocation id | `amz-sdk-invocation-id` | A per-call identifier retained in the record. **Not** a CloudTrail join key — see the correction below. |

`violations()` treats any of the following as a failure: zero attempts, more
than one attempt, `attempt > 1`, `max != 1`, more than one distinct invocation
id, or a witnessed request carrying anything other than exactly one instance id.

### Two measurement facts that had to be established, not assumed

**`needs-retry` is a question, not an action.** It fires on *every* attempt,
including a 200 that succeeds — botocore asks "should I retry?" and the checker
answers no. A witness that counted these events as evidence of a retry would
fail a perfectly correct single dispatch. This was found by a test that
asserted zero violations on the happy path and received one. The event is
recorded as `retry_questions` and is **not** a violation; only the attempt count
and the `attempt=` header are evidence. Naming the field after what it measures
is deliberate: `retry_decisions` was the original name and it was wrong.

**`before-send` does not carry `params`.** At that phase the request is already
serialized, and the target is only present in the protocol-specific body
(EC2's query protocol serializes lists positionally as `InstanceId.1`,
`InstanceId.2`). The target is therefore read at
`provide-client-params.ec2.StopInstances`, which does carry `params`, with a
body-parsing fallback. Reading the target at `before-send` alone would have
produced a permanent, silent "0 instance ids" reading.

Consequence for readers: an absent `max` token is not evidence of an unknown
budget. Per `botocore/handlers.py:1091-1103`, `max` is emitted only when
present in the retries context, so `max` is absent for a correctly
single-attempt client. It is treated as "one attempt permitted", and a
contradicting retry would appear as `attempt=2` regardless. This asymmetry is
recorded here because *the absence of a signal must never be read as a pass
without a second, independent signal* — which is precisely why the falsification
test exists.

### The witness is built to be falsifiable

A witness that only ever reports "one dispatch" proves nothing: it would report
one whether or not the SDK had retried, which is the failure it exists to catch.

So `tests/test_m15f_dispatch_witness.py` contains, as its primary content:

1. A correctly configured client producing exactly one witnessed attempt with
   no violations.
2. A **deliberately unsafe** client (`total_max_attempts=3`) driven with throttling
   responses, where botocore really does send multiple requests. The witness must
   report the violation and refuse to certify. If it does not, the witness is
   broken.
3. A **meta-test** pinning the premise of (2): it asserts botocore really did
   retry. Without it, a future botocore that stopped retrying would make (2) pass
   for the wrong reason — one attempt observed, conclusion drawn anyway — and the
   falsification would be silently worthless.
4. A synthetic second attempt with no `needs-retry` decision, proving the
   witness counts attempts rather than retry *decisions*. A count of retry
   decisions would miss a retry performed by anything other than botocore's own
   logic.
5. Zero attempts treated as a violation, never as a pass, because "nothing was
   sent" and "the counter is broken" must not be confused.
6. A 500 response under the correct configuration: exactly one attempt,
   confirming the configuration is what prevents the retry and not the response
   happening to be unretryable.
7. A 4xx rejection: one attempt, no violation. The invariant is about the *number
   of dispatches*, not about success. A rejection sent once and retried zero
   times is correct behavior and the witness must not call it a safety problem.
8. A misspelled event name (`StopInstance`, no trailing `s`), which botocore
   accepts, stores, and never emits. Asserted to **fail closed**: the witness
   arms, records nothing, and the zero-attempt rule refuses to certify. This
   pins a limit worth stating plainly — the registry check in `arm()` proves a
   listener reached the emitter, but `prefix_search` locates a handler by the
   name it was registered under, so it **cannot** distinguish a well-formed
   event name from a typo. Typo detection comes from the fail-closed zero rule,
   not from attachment verification.
9. Two refusal paths in `arm()`, each with a positive control proving it fires:
   an emitter exposing no searchable registry, and an emitter that drops one
   specific registration while accepting the others. Without those controls they
   would be untested branches inside a safety path.

All of this runs against real botocore with the transport stubbed at
`before-send`, which is the last hook before the socket. Client creation and
response parsing run for real; signing, header injection, and the retry state
machine all execute. No socket is opened.

That last claim is itself tested rather than asserted:
`tests/test_m15f_no_network.py` re-runs the M15-F suite in-process with
`socket.connect`, `socket.getaddrinfo`, and `create_connection` replaced by
functions that raise, and with every `AWS*` environment variable removed. Two
control tests confirm the blocking is real — that `getaddrinfo` raises, and that
a connection to the EC2 endpoint and to the instance-metadata address raises —
because a no-op blocker would produce a permanently green hermeticity gate, the
same "absence read as a pass" shape this milestone exists to remove.

### The witness is passive

It registers listeners and reads. It has no code path that issues a request, so
arming it cannot itself dispatch anything. A test parses the module's AST and
asserts it contains no call to `stop_instances` or `describe_instances`.

### The witness is a self-report, and cannot corroborate itself

Recorded here because ADR 0011 had to correct a claim this document made.

The witness observes **what the dispatching process sent**. If the process is
wrong, if the observer is defective, or if the bytes took a path the observer
cannot see, it will report one clean attempt regardless. Nothing about it is
independent of the thing it observes.

It is therefore the *first* of two evidence domains, not the whole of the proof.
AWS-side CloudTrail evidence is the second and can contradict the first — see ADR
0011, which also supersedes this document's description of
`amz-sdk-invocation-id` as a CloudTrail join key. CloudTrail does not reliably
carry that value, so matching on it would find nothing and read as a clean
negative.

What the witness uniquely provides, and CloudTrail cannot, is the client's own
attempt numbering and permitted budget from the SDK's headers. Together the two
domains answer "what did the client intend to send" and "what did AWS process",
which are not the same question.

## Role attachment: the decision rule, not the decision

The ruling defers task-role versus `sts:AssumeRole` until the execution
environment is known, and states a preference for a dedicated role attached
directly to the execution environment, avoiding an extra mutable permission
boundary.

M15-F records this as a **decision rule** rather than implementing either:

> Determine first where the executor actually runs. If that environment supports
> a task role, instance profile, or equivalent direct attachment for the execution
> identity, attach the dedicated mutation role there. Use `sts:AssumeRole` only
> when no direct attachment is available, and record why.

Neither assumption is implemented. The factory continues to accept explicit
credentials and no profile (ADR 0009), which both attachment mechanisms can
supply, so this decision does not block the client and is not blocked by it.

## Items 1–4: environment, credentials, target, attachment

1. **Where the execution process runs.** Not yet determined, and deliberately so.
   This is the input the credential decision depends on, so establishing it comes
   first. ADR 0010 does not assume an answer.

2. **Task role vs. `sts:AssumeRole`** — recorded as a *decision rule*, neither
   implemented:

   > Determine first where the executor actually runs. If that environment
   > supports a task role, instance profile, or equivalent direct attachment,
   > attach the dedicated mutation role there. Use `sts:AssumeRole` only when no
   > direct attachment is available, and record why.

   This matches the stated preference for avoiding a second mutable permission
   boundary (`environment → role → EC2` rather than
   `environment → STS AssumeRole → role → EC2`). Both paths supply the explicit
   credentials the factory already requires, and the factory exposes no profile
   parameter (ADR 0009), so neither is implemented here and neither is blocked by
   that module.

3. **Exact sacrificial instance requirements.** Runbook section 1, now eighteen
   preconditions: the original twelve plus witness-armed, policy-still-valid,
   retry-config-read-at-dispatch, the separate-forensic-reader and
   declared-correlation-window conditions (ADR 0011), and the CloudTrail
   region/trail identity (decision record 0012). The three mechanical ones are
   enforced by `sws_agent.preflight`; the rest stay human steps.

4. **Exact IAM attachment mechanism.** Deferred with item 2. The artifact, its
   scope, and its validator are settled (ADR 0009); only the attachment vehicle is
   open.

## Preflight is a gate, not a checklist

The runbook's preconditions (section 1) are necessary but currently manual: a
human reads them. M15-F's position is that any condition whose failure would
change the safety conclusion should be mechanically checkable, because a
forgotten checkbox and a satisfied checkbox look identical at the moment of
dispatch.

New module `sws_agent.preflight` evaluates those conditions and **cannot
dispatch**: it contains no call to `stop_instances`, no session construction,
and no import of an AWS SDK transport. A preflight that could issue a mutation
would be one more way to mutate, and the milestone's claim is that exactly one
dispatch happens — deliberately, after review. Two tests read the module's
source and assert this.

### Two phases, not one

The gate is split, because a single function could not be both:

- `evaluate_preflight(...)` — everything checkable **before** dispatch: target
  scoping, IAM policy validity, effective retry configuration, witness
  attachment, witness start state, and program invariants.
- `evaluate_dispatch_evidence(...)` — everything checkable **after** dispatch,
  from the witness alone: that the witnessed targets are the pinned instance,
  and that the witness reports no violation.

Pre-dispatch the witness has observed nothing, so its *content* cannot be
checked. Post-dispatch the target and program state are stale. Merging them
would force one phase to be wrong, so the split is asserted by a test that
checks the two report disjoint check names and that the post-dispatch phase
refuses a witness with no records.

Every finding records `detail` **and** `evidence`, so a reader can tell a
verified condition from an unchecked one. A finding that cannot be measured
fails: an unreadable retry configuration is a refusal, never an optimistic
default.

### What the gate deliberately does not cover

Target state, approval freshness, `DryRun` outcome, and credential provenance
all require AWS access, so they remain runbook steps for humans. This is
recorded explicitly so the gate is not mistaken for a replacement of the
runbook — a reader who believed the automated gate covered approval freshness
would skip the check.

The CloudTrail cross-check is also not in the gate: it needs AWS access, and a
check that silently skipped it would present partial evidence as complete.

### Every check can fail, and that is tested

`test_the_gate_can_fail_on_every_check_independently` varies exactly one input
per check and asserts that check refuses. A gate whose checks could only pass
would be an illusion of width, and this milestone has already had to correct
one "absence read as a pass" mistake (the `needs-retry` misreading); the same
failure shape is worth testing for here too.

## A correction to an M15-E invariant

ADR 0009 and its test asserted that *no module under `src/sws_agent` imports*
`ec2_mutation_client`, so nothing outside it could obtain a mutation client.
M15-F broke that assertion, and the assertion was wrong rather than the import.

`preflight.py` imports `effective_retries`, `validate_iam_policy`, and
`MutationClientSettings` — a settings dataclass and two pure functions. None of
them can construct a client. The property that matters is *acquisition*, not
*import*, and the check now detects the former.

The check parses imports with `ast` rather than matching text. Regex was tried
first and failed in both directions: a bare substring cannot distinguish a
docstring mention from an import, and a line-anchored pattern misses a
parenthesized multi-line import — which a module importing several names would
plausibly write. Both the missed import and the false positive were found by
this check misfiring during M15-F, which is the usual fate of security regexes:
they pass until they don't.

Three tests pin the detector: every real acquisition form must be caught
(including multi-line, nested, aliased, star, and function-local imports);
inspection-only imports of the same module must be allowed; and prose mentions
must not trip it. A fourth test requires every module in the package to import
cleanly, so the detector's "unparseable means safe" fallback cannot hide a
syntax error.

## The MCP pin, and an ignored argument

`check_program_invariants(implemented, tool_names)` was written to fail if an
action had been flipped to `implemented` **or** an MCP tool had been added. It
checked only the first. `tool_names` was accepted, named in the docstring, and
then never read — and because every caller passes the real nine names, no test
could surface the gap. The argument looked handled and the gate silently did half
of what it claimed.

The fix pins the nine expected names as `EXPECTED_MCP_TOOL_NAMES` inside
`preflight` and compares them by set. Pinning locally rather than importing
`sws_agent.mcp.server.BUILTIN_TOOL_NAMES` keeps the gate from importing the MCP
stack into the module whose job is to check that stack; the duplicated constant is
paid for by a test asserting the pin still equals the real set, so drift fails
there rather than at the gate.

Set equality rather than a count, deliberately: nine arguments where one is
duplicated and `request_approval` is missing is a surface of eight, and a
`len(...) == 9` check would pass it. A duplicate on its own is *not* a defect — a
surface is a set, and a name repeated is the same surface — which is why that case
passes and the padded-over-a-missing-tool case does not.

Implementing this surfaced two of its own bugs, both caught by the existing
healthy-path test rather than by inspection: comparing a `sorted list` against a
`tuple` with `!=`, which is always true and made every preflight refuse; then
subtracting a `tuple` from a `set`. The first is worth recording — a check that
fails closed on *everything* still looks like a passing safety check in a report
that only shows the authorized boolean.

## What the live run must additionally capture

Beyond the runbook's existing evidence list:

| Evidence | Why |
| --- | --- |
| Witness record for the dispatch: attempt count, `attempt=`, invocation id | The single-dispatch claim itself |
| Witness report after the run: `violations() == ()` | Negative result, stated affirmatively |
| CloudTrail `StopInstances` events matched by principal + session + target + window | Corroboration from outside the process, so a witness defect cannot produce a false pass. See ADR 0011. |
| The client config actually in force at dispatch, not at construction | A client reconfigured between construction and dispatch is a real possibility |

CloudTrail is read with a **separate** read-only forensic role, as the runbook
already requires. The mutating role must not hold it: the credential that can
change things must not be able to inspect itself.

## Abort criteria

Any of these ends the run before or during dispatch, and none is recoverable by
retrying:

- The witness reports any violation at any point.
- The effective retry configuration is anything other than one attempt.
- The target ARN, instance id, or policy scope disagrees with the pinned target.
- `DryRun` returns anything other than `DryRunOperation`.
- Pre-state is anything other than `running`.
- The approval is spent, stale, or self-approved.
- More than one instance appears in any response.
- Any MCP surface change appears during the run.

A CloudTrail count greater than one for the intended principal, session and
target is an incident, not a data point: it means the safety chain failed and the
program stops. ADR 0011 makes this the deciding rule — where the witness and
CloudTrail disagree, the AWS-side evidence is the fact.

## Items 5–14: where each one is settled

The ruling enumerated fourteen items. Rather than leave a reviewer to hunt for
each, here is where each landed — including the two that remain deliberately
open.

| # | Item | Where it is settled | Status |
| --- | --- | --- | --- |
| 1 | Executor location | "Items 1–4", above | Open — the input to #2 |
| 2 | Task role vs. `AssumeRole` | "Role attachment", above | Decision rule recorded; neither implemented |
| 3 | Sacrificial instance requirements | Runbook §1, eighteen preconditions | Settled |
| 4 | IAM attachment mechanism | ADR 0009 for artifact/scope/validator | Deferred with #2 |
| 5 | Credential provenance | Runbook §1 preconditions; factory takes explicit credentials with no profile parameter (ADR 0009) | Settled for mechanism; evidence captured at run time |
| 6 | Pre-stop observation | Runbook §1; must read `running` | Settled |
| 7 | `DryRun` probe | Runbook; one-shot, `total_max_attempts=1`, expects `DryRunOperation` | Settled |
| 8 | Exact mutation invocation | ADR 0008 handler, pinned params, one target; witnessed by this ADR | Settled |
| 9 | Post-stop settle evidence | ADR 0006 frozen semantics; runbook §9 | Settled |
| 10 | Abort criteria | "Abort criteria", above | Settled |
| 11 | `UNKNOWN` handling | "UNKNOWN handling", below | Settled |
| 12 | Cleanup / termination | Runbook §11 | Settled |
| 13 | Proof of no second dispatch | `DispatchWitness` + `evaluate_dispatch_evidence` | Settled and tested |
| 14 | No broadening of target or permissions | Runbook §1 rules 1 and 3; `validate_iam_policy`; the gate's `target_scoping` | Settled, mechanically enforced |

Two of the fourteen (#1 and #4, with #2) remain open because they depend on
where the executor runs. They are recorded as open rather than assumed, and
neither blocks the parts of the design that are settled.

## UNKNOWN handling

Unchanged from ADR 0006 and the runbook: `DISPATCH_UNKNOWN` and any
unestablished outcome are terminal. No compensating mutation, no re-run, no
approval re-creation. The witness adds one thing — in an `UNKNOWN` case the
witness record and CloudTrail count are the *only* evidence that distinguishes
"sent once, outcome unknown" from "never sent," and both are collected before
any decision is discussed.

## What M15-F deliberately does not do

- It does not authorize or perform the live stop.
- It does not attach the IAM policy, create a role, or install credentials.
- It does not create or terminate any instance.
- It does not set `STOP_RESOURCE.implemented = True`.
- It does not change MCP.
- It does not implement either credential-attachment mechanism.
- It does not commit or push.

The first live stop remains a separate authorization, with its own preflight
review, because it deserves its own preflight, evidence capture, abort criteria,
and post-execution review.