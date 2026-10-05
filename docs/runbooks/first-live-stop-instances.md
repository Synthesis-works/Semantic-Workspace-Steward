# Runbook: the first live `ec2:StopInstances`

- **Status**: Draft for a future milestone. **Not executable today.** `STOP_RESOURCE.implemented` is `False`, no mutating handler is registered, and MCP exposes exactly nine tools. Nothing in this document runs until a later milestone implements ADR 0006 and this runbook is executed under its own human authorization.
- **Authority**: ADR 0006 (`docs/adr/0006-stop-instances-dispatch-evidence-and-settle.md`); design and the single-dispatch invariant in ADR 0010 (`docs/adr/0010-first-live-stop-preflight-design.md`); AWS-side verification and reconciliation in ADR 0011 (`docs/adr/0011-cloudtrail-independent-verification.md`)
- **Scope**: one stop, of one purpose-created sacrificial EC2 instance, performed once, under observation, with the evidence retained.
- **Exposure**: the mutation stays **private and local**. It is not reachable through MCP, and MCP remains at nine tools no matter what this runbook produces.
- **Prerequisite — blocking**: `docs/decisions/0012-first-live-stop-environment-decision-record.md` must be populated and reviewed **before any step of this runbook is attempted**. That record fixes the executor environment, the mutation principal, the separate forensic reader, the CloudTrail lookup region and trail identity, and the delivery policy. This runbook does not choose any of them, and none of them can be inferred at the console. **The live run is blocked until every required field in that record is filled, every gate checkbox in it is satisfied, and the live mutation is separately and explicitly authorized.** A populated record is necessary but not sufficient: it does not authorize anything by itself.

## 0. What this runbook is for

Every irreversible step in SWS so far has been theoretical. This is the procedure for the first one, and it is written to be read by someone who has not read the code. It assumes the reader does not trust the tool.

Four rules govern the whole document and override anything in it:

1. **The target is one sacrificial instance.** If the instance in front of you is not the one named in section 1, stop. Nothing in this runbook is a reason to proceed with a substitute.
2. **Ambiguity is a stopping point, not a retry.** If the run reaches any state where the outcome is unknown, it stops and stays stopped. Re-running `StopInstances` is never the remedy for not knowing. See section 10.
3. **The run must prove one dispatch, not merely a stopped instance.** A stop that was dispatched twice also produces a stopped instance. Section 8.3 is not optional bookkeeping; it is the point of the exercise.
4. **AWS-side evidence decides.** The local witness says what this process sent; CloudTrail says what AWS received and processed. Where they disagree, CloudTrail is the fact and the run is unsafe, not the witness (section 12).

---

## 0.1 The sequence

The order below is load-bearing. Two consequences deserve emphasis before anything else.

**CloudTrail is not a prerequisite for dispatch.** It cannot confirm a dispatch that has not happened, so requiring it before `AUTHORIZE` would be circular and the run could never start. It is independent *post-dispatch* evidence, and section 12 is where it belongs.

**A run that cannot be reconciled is unresolved.** If CloudTrail is unavailable, or cannot establish the event unambiguously, the run is *unresolved* — never "probably fine", and never downgraded because the local witness looked clean.

```
PRECHECK                 §1-§7   identity, target, policy, shape
      ↓
DRY RUN                  §7.1    DryRunOperation proves authorization
      ↓
AUTHORIZE SINGLE DISPATCH §8     preflight gate, all findings measured
      ↓
STOP_INSTANCES                    exactly one attempt; no MCP surface
      ↓
LOCAL WITNESS             §8.3    what this process sent
      ↓
SETTLE / OBSERVE          §9      15s × 40; only `stopped` is success
      ↓
CLOUDTRAIL                §12     what AWS received and processed
      ↓
RECONCILE                 §12.1   agreement, or an incident
```

The witness is read *before* CloudTrail so its record exists even if the
reconciliation step fails, but the reconciliation verdict is never derived from
it alone.

---

## 1. Sacrificial target preconditions

All eighteen must hold. Record each as pass/fail with evidence. Any failure ends the run before any credential is used.

| # | Precondition |
| --- | --- |
| 1 | The instance is purpose-created for this validation and has no other purpose. |
| 2 | It is disposable. Terminating it loses nothing of value. |
| 3 | Nothing depends on it: not a load balancer target group, not an Auto Scaling group, not a scheduled job, not a monitoring check, not a Route 53 record. |
| 4 | It holds no data. Any attached volume is delete-on-termination and empty. |
| 5 | It has no IAM instance profile and no credentials to reach anything. |
| 6 | No public IP. No inbound rule permits SSH or anything else. |
| 7 | It is tagged `SwsPurpose=sacrificial-stop-validation`, `SwsOwner=<operator>`, `RunId=<uuid>`. |
| 8 | Its instance id, account id, region, and current state are written down here before anything runs. |
| 9 | The mutating role is scoped to this exact instance ARN (ADR 0009 IAM section; `DescribeInstances` on `*` is the owner-ratified, unavoidable read-only overbreadth for this one role). |
| 10 | The read-only collection principal is unchanged and does not hold `ec2:StopInstances`. |
| 11 | A named human has authorized this specific run, in writing, with the instance id in it. |
| 12 | The operator has read section 10 and knows what an ambiguous outcome requires. |
| 13 | The dispatch witness arms successfully against the actual client, before dispatch (section 8.3). An unarmed witness records nothing and cannot distinguish "nothing sent" from "counter broken". |
| 14 | The IAM artifact still validates and is still scoped to this one ARN (`validate_iam_policy`), and the ARN matches the one written below. |
| 15 | The client's effective retry configuration reads as one attempt **at dispatch**, not merely at construction. |
| 16 | A **separate** read-only forensic role is available for the section 12 CloudTrail read, and the mutating role has been confirmed **not** to hold CloudTrail read access. The reconciliation refuses evidence whose reader is the mutating principal, so this cannot be satisfied by self-inspection. |
| 17 | The correlation window for section 12 is defined in advance and is timezone-aware. Events for the target falling outside it will fail the reconciliation — the window is recorded, not chosen afterwards to make the result look clean. |
| 18 | The CloudTrail **lookup region and trail identity** are fixed in decision record 0012 (Decision 4), and confirmed to cover the target account and region. `LookupEvents` is regional: a zero result from the wrong region returns clean and empty, so region selection is a precondition rather than a detail. See section 12. |

Instance id: `i-___________________`  Account: `___________`  Region: `__________`  State at authorization: `________`

Authorization: `<name>`, `<date/time UTC>`

If the instance has been running longer than the validation window expects, note it; a long-lived instance is still disposable, but its `PreviousState` in the stop response is then more informative, not less.

---

## 2. Environment and account verification

Confirm the shell is pointed at the intended account before any client is constructed. A credential that resolves elsewhere is the failure this step exists to catch.

```powershell
aws sts get-caller-identity --output json
```

Record: `Account`, `Arn`, `UserId`.

Refuse to continue unless `Account` matches the account in section 1 **exactly**. A mismatch is a stop, not a correction — re-derive the intended target before proceeding.

Also record `aws configure get region`. A region set in the environment but not in the client configuration is a discrepancy worth resolving now rather than at dispatch.

---

## 3. Credential and role verification

The mutating client takes its credential explicitly. There is no fallback to an environment variable, a shared profile, or an instance-profile role.

```powershell
aws sts assume-role --role-arn <mutating-role-arn> --role-session-name sws-stop-<runid> --external-id <external-id> --output json
```

Record the returned `AssumedRoleUser.Arn` and the `Credentials.Expiration`.

Two things are checked here and neither is obvious:

- **The session must expire well after the settle deadline.** The settle window is 600 seconds. A session with less than 15 minutes of remaining life is refused, because an expired session mid-settle produces an observation error indistinguishable from a network fault.
- **The session name is recorded.** It is the join key between SWS's audit ledger and CloudTrail. Without it, an ambiguous outcome has no way to find its own event.

Do not proceed if the role can be assumed by anything other than the private CLI's principal.

---

## 4. Region verification

```powershell
aws ec2 describe-instances --region <region> --instance-ids <instance-id> --output json
```

Refuse unless the response contains exactly one reservation with exactly one instance, and that instance's id matches section 1.

A response with zero instances means the id does not exist in this region. This is a terminal fact about the target, not an observation failure, and it ends the run.

---

## 5. Pre-state observation

From the section 4 response, record:

| Field | Value |
| --- | --- |
| `InstanceId` | |
| `State.Name` | |
| `State.Code` | |
| `PrivateIpAddress` | |
| `SubnetId` / `VpcId` | |
| `LaunchTime` | |
| `Tags` | must include the three from precondition 7 |

Expected `State.Name` is `running`. If it is already `stopped`, the run is unnecessary — stop. If it is `pending`, `stopping`, `shutting-down`, or `terminated`, the run is invalid for this target — stop and record the state.

This observation is the freshness evidence. The approval in section 6 is bound to it, and ADR 0006 re-observes immediately before dispatch to close the window between here and the effect.

---

## 6. Approval and execution-intent verification

The approval must already exist and be unspent. This runbook does not create one; it consumes one that a human made in advance.

Verify, from the approval ledger, before dispatch:

- The intent is `STOP_RESOURCE` against the instance id in section 1.
- The ticket revision recorded in the approval equals the live revision. A stale revision is refused by the gate and must not be worked around.
- The approval is unspent and the intent has no `RESERVED`, `ATTEMPTED`, or `UNRESOLVED` execution.
- The approver is a human, and is not the operator running the validation. Self-approval defeats the control.
- The approval does not predate this run's authorization by more than a short, stated interval.

If any check fails, the gate refuses. That refusal is the system working.

---

## 7. Request shape

The request is constructed here in full so it can be reviewed before it exists in code.

```
ec2:StopInstances
  Region          : <pinned region>
  InstanceIds     : [ <one id> ]        # exactly one; length is asserted, not assumed
  Force           : false
  Hibernate       : false
  SkipOsShutdown  : false
  DryRun          : false               # preceded by the DryRun probe in 7.1
```

Every optional member is stated explicitly. `Force`, `Hibernate`, and `SkipOsShutdown` are not left to defaults: each changes what the call means. `Hibernate: false` is additionally required for correctness — the EC2 state enum visible to `DescribeInstances` has no hibernated member, so a hibernated instance could never be observed as `stopped`.

### 7.1 DryRun probe

Immediately before the real call, with the same role, region, and parameters:

```powershell
aws ec2 stop-instances --region <region> --instance-ids <instance-id> --dry-run --output json
```

Expected: `DryRunOperation` (HTTP 403). This confirms the role is authorized for `ec2:StopInstances` on this instance and that the parameters are valid, without applying anything.

Any other result is a stop:

| Result | Meaning |
| --- | --- |
| `DryRunOperation` (403) | Authorized and valid. Proceed to section 8. |
| `UnauthorizedOperation` / `AccessDenied` (403) | The role is not authorized for this instance. Fix the policy; do not work around it. |
| `InvalidInstanceID.NotFound` (400) | Wrong region, wrong account, or the instance is gone. Return to section 1. |
| `InvalidInstanceID.Malformed` (400) | The id is wrong. This is the exact wrong-target hazard ADR 0006 enumerates. Stop. |

---

## 8. Boundary evidence

Two records bracket the effect and both are captured before continuing.

**Before dispatch.** The ledger row is `ATTEMPTED` and the audit `CROSS` record is written. From this point the boundary is considered possibly crossed and nothing may re-execute the intent without the section 10 procedure.

**After the call returns.** Record the `StopInstancesResult` verbatim:

```
StoppingInstances[0].InstanceId       = <instance-id>
StoppingInstances[0].PreviousState.Name = running
StoppingInstances[0].CurrentState.Name  = stopping
```

This pair is the service's own statement of what it accepted. It is recorded before any `DescribeInstances` call, because it is the only direct evidence of the service's intent and it is what makes a first observation of `running` interpretable rather than alarming.

Branch on the disposition (ADR 0006, dispatch disposition):

| Outcome | Record | Then |
| --- | --- | --- |
| 200 with `StoppingInstances` | `ACCEPTED` | Section 9. |
| 4xx classified | `DISPATCH_REJECTED` | Section 8.1. |
| 5xx, transport failure, unrecognised code | `DISPATCH_UNKNOWN` | **Section 10.** |
| Raised before the request was written | `NOT_DISPATCHED` | Section 8.2. |

### 8.1 `DISPATCH_REJECTED`

Nothing was applied. Record the structured `Error.Code` and HTTP status, then stop. Re-attempting is permitted only after a human has fixed the cause, and only through the normal gate — never by re-running this runbook unchanged.

### 8.2 `NOT_DISPATCHED`

No credential, region, endpoint, or TLS problem. Nothing was applied and the intent is retryable once the environment is fixed. This is the one branch where fixing the cause and starting over is legitimate, because there is no ambiguity to reconcile.

---

## 8.3 Single-dispatch evidence

Proving the instance stopped is easy. Proving that only one request ever reached
the wire is a different claim, and a successful stop is not evidence for it — a
twice-dispatched stop also stops the instance.

`sws_agent.mutation_evidence.DispatchWitness` observes every `StopInstances`
attempt at botocore's sender. Arm it before dispatch and retain its record.

| Field | Why it is evidence |
| --- | --- |
| Attempt count | Number of HTTP requests attempted. Anything but 1 is a failure. |
| `attempt=N` from `amz-sdk-request` | The SDK's own numbering. `attempt=2` means a second attempt was made. |
| `max=M` from the same header | The SDK's own statement of the permitted budget. |
| `amz-sdk-invocation-id` | A per-call identifier retained in the record. **Not** a CloudTrail join key — see section 12. |

Record the report *after* the run as well as during it. `violations()` returning
an empty tuple is the negative result, and it must be written down as such —
"no violation observed" is not the same claim as "one dispatch occurred", and
only the first is available before the CloudTrail cross-check in section 10.2.

Two facts about this evidence, established against botocore 1.43.101 rather
than assumed, both in ADR 0010:

- **`needs-retry` is a question, not an action.** It fires on every attempt,
  including a successful 200. It is not evidence of a retry. Only the attempt
  count and the `attempt=` header are.
- **An absent `max` token is not an unknown budget.** botocore emits `max` only
  when present in the retries context, so a correct single-attempt client has
  none. A contradicting retry would appear as `attempt=2` regardless.

Any violation aborts the run. So does a CloudTrail count greater than one for the
intended principal, session and target — that is an incident, not a data point,
and it means the safety chain failed. Where the witness and CloudTrail disagree,
the AWS-side record is the fact (section 12.1).

---

## 9. Bounded polling and final classification

Poll `describe-instances` every 15 seconds to a 600-second deadline, recording one audit row per poll with attempt id, instance id, account, region, observed state, and observed timestamp.

Stop polling on the first of:

| Observed | Classification | Recorded outcome | Re-executable |
| --- | --- | --- | --- |
| `stopped` | success | `VERIFIED_SUCCESS` | n/a |
| `shutting-down`, `terminated`, `pending`, instance absent | terminal contradiction | `FAILED` | **no** |
| deadline reached while `stopping` or `running` | not established | `UNKNOWN` → `UNRESOLVED` | **no** — section 10 |
| 3 consecutive observation errors | not established | `UNKNOWN` → `UNRESOLVED` | **no** — section 10 |

The distinction in the last two rows is the point of the whole exercise. `stopped` is the intent and the only success. An instance that is `stopping` at the deadline is **not** a failure — AWS reported healthy progress — and **not** a success either, because nothing confirmed arrival. `UNRESOLVED` says exactly that, and it blocks re-execution, which is the correct posture.

An instance that never left `running` is different: there is no evidence the stop took any effect, so `FAILED` is accurate and a retry is legitimate.

---

## 10. Abort and `UNKNOWN` procedure

**Any ambiguous outcome ends the run and stays ended. Do not re-run `StopInstances`.**

Applies when: the disposition is `DISPATCH_UNKNOWN`; or the outcome is `UNKNOWN` for any reason — transport failure, deadline expiry in a progressing state, or repeated observation errors.

### 10.1 Prohibited

- Re-running the stop, "just to see if it works this time".
- Re-creating the approval to obtain a fresh execution.
- Editing, deleting, or backdating any ledger or audit record.
- Any compensating mutation — starting the instance, stopping something else, terminating anything — to "resolve" the ambiguity.
- Letting the reconciliation verdict be overridden in favour of the local witness. Where they disagree, CloudTrail is the fact (section 12.1).

### 10.2 Evidence to collect

Every field, before any decision is discussed:

| Field | Source |
| --- | --- |
| Execution id, attempt id, reservation id | execution ledger |
| Intent key, ticket revision | approval ledger |
| Instance id, account, region | pinned target record |
| Dispatch disposition and any structured `Error.Code` | audit `CROSS` / result record |
| `PreviousState` / `CurrentState` from the stop response, if received | AWS response |
| Every poll record: state and timestamp | audit settle records |
| Role session name and session expiry | section 3 |
| Witness record: attempt count, `attempt=`, `max=`, invocation id | section 8.3 |
| Witness `violations()` report, including if empty | section 8.3 |
| `CloudTrail` `StopInstances` events: principal, session name, target, timestamps, `errorCode` | read-only forensic role |
| Reconciliation report: outcome, `aws_side_count`, every finding | section 12.1 |

The witness and CloudTrail are both required, and they are deliberately not the
same evidence. The witness counts from inside the process that dispatched; if it
were defective, only an external source could contradict it. In an `UNKNOWN`
case these two rows are what distinguish "sent once, outcome unknown" from
"never sent", so neither may be skipped.

The `CloudTrail` lookup uses a **separate** read-only forensic role. The mutating role must not hold `DescribeTrails` or CloudTrail read access; granting it would widen the credential that can change things so that it can also inspect itself. `reconciliation.py` refuses evidence whose reader is the mutating principal, so this cannot be satisfied by self-inspection even accidentally.

Correlation is by **principal + session name + target + time window**, not by
the SDK invocation id. The invocation id is recorded as a witness-side detail;
CloudTrail does not reliably carry it, and a cross-check that quietly matched on
nothing would report "no events found" and read as a clean negative.

### 10.3 Decision paths

After evidence is in hand, a human decides. These are the only shapes the decision takes:

| Evidence | Reading | Action |
| --- | --- | --- |
| Event exists; final observed state `stopped` | The stop succeeded; only the confirmation was lost | Close as success by human record. Leave `UNRESOLVED` in the ledger as the truthful machine fact. |
| Event exists; state `stopping` at deadline | Converging | Wait and observe **read-only** until it settles. Record the result. Still no re-run. |
| Event exists; state `running` | The request was recorded but did not take | Raise a **new** intent with a **new** approval, justified by this evidence. That is a fresh authorization, not a retry. |
| No event, from a **settled** query against the correct region and trail (section 12) | The request was not processed by AWS | Correct the cause, then a new intent with a new approval. |
| No event, from an **early** query, or from an unconfirmed region/trail | Not a finding — the query was not yet capable of finding one | **Unresolved.** Re-query under section 12. Do not read an empty result as "no event". |
| CloudTrail **unavailable**, or cannot be attributed | Not a finding — an absence of evidence | **Unresolved.** Do not treat as "no event" and do not close the run. |
| AWS recorded **more than one** event | The mutation was processed more than once | **Incident.** AWS-side evidence wins over the witness; see section 12.1. |
| Event exists for a **different** instance id | Wrong target | **Stop the entire program.** The pinning controls failed. Do not proceed to any further live write until ADR 0006's target-pinning section is revisited. |

The distinction between the fourth and fifth rows is the one the ruling turns
on. "No event" is a fact you can only have if you were able to look — *late
enough, in the right place*. Both halves are required, and neither is a property
of the query succeeding.

### 10.4 Reporting

An `UNKNOWN` is a finding, not an embarrassment. It is recorded in the ledger as `UNRESOLVED` and stays that way permanently — `UNRESOLVED` has no outgoing transition and no automatic override, which is the property that makes stopping safe.

---

## 11. Evidence retained

Retained for every run, success or not:

- Section 1 preconditions with per-item pass/fail and the authorization.
- `sts get-caller-identity` output; assumed-role session name and expiry.
- Pre-state observation with timestamps.
- Approval and intent verification results.
- The `DryRun` probe result.
- The stop request as sent, and the `StopInstancesResult` verbatim.
- Every settle poll record.
- Witness record and violations report (section 8.3).
- The correlation window actually used (section 12).
- Every CloudTrail delivery attempt: attempt number, timestamp, lookup region,
  trail, parameters, and result (section 12). The attempt series is what
  establishes query maturity; a single result does not.
- `CloudTrail` events in that window: full records, not just a count — principal,
  session name, targets, timestamps, `errorCode`, event ids.
- Reconciliation report: outcome, `aws_side_count`, and every finding row
  (section 12.1). An empty `failures` list is itself evidence and is retained.
- The client's effective retry configuration **as read at dispatch**, not as
  constructed. A client reconfigured between construction and dispatch is a real
  possibility, and the handler's precondition is what catches it.
- Final classification, ledger state, and outcome.
- Any `UNKNOWN` evidence set from 10.2 and the resulting human decision.

A count is not a record. "One event" with no principal, no timestamp, and no
target cannot be reconciled by anyone reading it later, and an unreconcilable
retained artifact is indistinguishable from no artifact at all.

The audit ledger is evidentiary, not authoritative. It records what happened; it does not decide what may proceed. That separation is ADR 0005 and it is unchanged by this runbook.

---

## 12. CloudTrail cross-check

This step is **independent verification**, not documentation. The local witness
observes what the dispatching process sent; CloudTrail records what AWS received
and processed. They are different evidence domains, produced by different
systems, and only the second one can contradict the first.

Read it with the **separate read-only forensic role** (section 3). The mutating
role must not hold CloudTrail read access, and `reconciliation.py` refuses
evidence whose reader is the mutating principal.

### Delivery maturity — read this before 12.1

**CloudTrail delivery is asynchronous.** An event that AWS has already accepted
and processed may not be queryable for some time afterwards. A lookup performed
shortly after dispatch can therefore return a clean, empty, successful result for
a request that was in fact processed exactly as intended.

Two independent things can produce that empty result:

- **The event has not been delivered yet.** Normal, expected, and indistinguishable
  from the next case at the moment of the query.
- **The query is pointed at the wrong region or trail.** `LookupEvents` is
  regional. A wrong-region query also returns clean and empty, forever.

So an initial zero-result lookup is **not independently dispositive**. It is
consistent with "processed once", with "never processed", and with "looked in the
wrong place", and nothing in the response distinguishes the three.

The only thing that makes a zero result dispositive is **query maturity**: a
query made late enough that delivery would have occurred, against a region and
trail already confirmed to cover the target.

**The currently executable policy is A — bounded wait and re-query**, as recorded
in decision record 0012 (Decision 5). Under it:

1. Query once immediately after settle completes. Record the result either way.
2. If it yields the expected event, stop. Maturity no longer matters.
3. If it yields nothing, wait the recorded interval and re-query, up to the
   recorded maximum number of attempts.
4. **Record every attempt as evidence** — attempt number, timestamp, region, trail,
   parameters, and result. The series is the evidence that the final empty result
   came from patience rather than from never looking in the right place.
5. **Only the final, settled query may support a zero-event conclusion.** An early
   empty result supports nothing on its own.

**What this procedure does not do.** The module does not enforce maturity, and
this is accepted rather than overlooked. `CloudTrailEvidence` carries no
query-timestamp or delivery-age field, so `reconcile_dispatch` cannot tell a
settled empty result from an early one. If the procedure above is skipped and an
early zero-result query is passed in, `AWS_SIDE_DISAGREEMENT` is the expected and
correct result from the module's point of view — and a false incident on a
perfectly healthy run.

**This is the control being accepted:** the wait-and-re-query procedure in this
section is the only thing standing between CloudTrail's delivery latency and a
manufactured incident. It is procedural, it is unaudited by any code, and it can
be skipped by someone who does not know it exists. That is why it is written here
rather than left to the module, and why decision record 0012 must record its
parameters explicitly instead of leaving them to judgement at the console.

Policy B in decision record 0012 — an explicit delivery-age input on
`CloudTrailEvidence` — would move this control into code where it cannot be
skipped. **It is not available**: those fields do not exist, and adopting it would
require unfreezing the implementation. It remains a field in the record, not an
option for this run.

**Correlation is by four attributes together**, and all four must match:

| Attribute | Value | Why |
| --- | --- | --- |
| `eventName` | `StopInstances` | Anything else is unrelated activity. |
| `eventSource` | `ec2.amazonaws.com` | Excludes same-named events from other services. |
| Principal | the intended execution principal ARN | A stop by anyone else is not this run. |
| Session name | the STS session name from section 3 | Separates this run from a concurrent one by the same role. |
| Targets | exactly the pinned instance id | A different target is a program-level failure. |
| Timestamps | within the recorded correlation window | See below. |

### 12.0 The correlation window

Record the window explicitly — it is the period from immediately before
`StopInstances` is called until settlement has finished. It must be
timezone-aware and stored with the evidence.

If any `StopInstances` event for the same target falls **outside** the declared
window, the window is wrong and the run cannot be attributed unambiguously.
`reconciliation.py` reports that as a failure rather than quietly ignoring the
event, because the alternative is a confirmation built on a window that
excludes the evidence against it.

### 12.1 Reconciliation

`sws_agent.reconciliation.reconcile_dispatch` compares the witness against
CloudTrail and returns exactly one of four outcomes. Three are failures.

| Outcome | Meaning | What happens |
| --- | --- | --- |
| `CONFIRMED` | Exactly one AWS-side event for the intended principal, session, target and window, and the witness agrees. | The single-dispatch claim is verified from outside the process. Proceed to close. |
| `AWS_SIDE_DISAGREEMENT` | AWS-side evidence contradicts the witness — different count, target, or principal. | **Incident.** AWS is the fact. Do not re-run. Section 10 applies. |
| `UNRESOLVED` | CloudTrail unavailable, errored, or unattrributable. | **Unresolved.** Not "probably fine". Do not close the run. |
| `NO_DISPATCH_OBSERVED` | Both sides agree nothing was dispatched. | A verified absence, not a confirmation of one dispatch. Retrying is legitimate here — and only here — because there is no ambiguity. |

**Precedence is not negotiable.** Where the witness and CloudTrail disagree, the
AWS-side record determines what happened. A clean witness against two AWS-side
events is an incident, not a near-miss; the module is tested against exactly
that case because it is the one where a reasonable implementer would be tempted
to prefer the local view.

`require_reconciliation` raises unless the outcome is `CONFIRMED`, so a caller
cannot treat "the reconciliation ran" as "the reconciliation passed". That
distinction is the whole point and is asserted in tests.

**Unavailability is never inferred as absence.** `CloudTrailEvidence` keeps
`available` separate from `events`, because a failed query and a query that
legitimately found nothing both present an empty tuple. The first is
`UNRESOLVED`; the second is `AWS_SIDE_DISAGREEMENT` when the witness claims a
dispatch. Collapsing them would make every outage read as "no events, therefore
fine".

**Nor is an immature query.** `available=True` means the query *ran*, not that
delivery had occurred. A settled query is what makes "zero events" a fact rather
than a guess — see the delivery-maturity section above, which governs whether this
verdict may be drawn at all.

**Reconciliation is not a dispatch prerequisite.** It cannot confirm a dispatch
that has not happened, so requiring it before `AUTHORIZE` would be circular. It
runs after `STOP_INSTANCES`, always, and `sws_agent.preflight` must never
import it — asserted structurally, so the cycle cannot be reintroduced quietly.
