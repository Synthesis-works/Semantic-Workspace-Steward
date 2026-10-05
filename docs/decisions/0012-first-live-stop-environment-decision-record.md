# M15 — Environment Decision Record

**Status: DRAFT — NO LIVE MUTATION AUTHORIZED — ENVIRONMENT BLOCKED**

- HEAD: `39fac2c`
- Scope: first live `STOP_RESOURCE` execution
- Implementation status: **no code changes authorized by this record**
- Related: ADR 0011 (AWS-side verification and reconciliation), ADR 0010
  (single-dispatch witness and preflight), runbook
  `docs/runbooks/first-live-stop-instances.md`

## Evidence sources used

Facts below come from read-only inspection on2026-10-05. No AWS mutation was
performed; no code, test, or IAM artifact was modified.

| Source | What it establishes |
| --- | --- |
| Repository scan for IaC (Terraform/CDK/SAM/Ansible/Docker) | **None present.** The repository defines no AWS infrastructure, account, region, role, or trail. |
| Repository scan for 12-digit account identifiers | Every occurrence is a **synthetic test fixture** (`123456789012`, `210987654321`, `012345678901`, `999999999999`) or a docstring example. No real account id is recorded in source. |
| `%USERPROFILE%\.aws\config` | Exists. Profiles `opencode` and `default`, both `region = us-east-1`, both `login_session = arn:aws:iam::527557823928:root`. |
| `%USERPROFILE%\.aws\credentials` | **Does not exist.** No static credentials file. |
| `AWS_*` environment variables | **None set.** |
| `aws sts get-caller-identity --profile opencode` (read-only) | Account `527557823928`, identity **`arn:aws:iam::527557823928:root`**. |
| `aws cloudtrail describe-trails --region us-east-1` (read-only) | `{"trailList": []}` — **no trail exists** in this account/region. |

Two blockers below are established *negative* facts, not gaps in investigation.
Account-wide enumeration (for example `iam:ListRoles`) was deliberately **not**
performed: it is a broad discovery scan, out of scope for this record, and it
would not change either blocker.

## Purpose

Capture the environmental decisions required before the first real EC2
`StopInstances` execution.

This record does **not** authorize execution by itself. The live gate remains
closed until all required fields are populated, reviewed, and explicitly
authorized.

The decisions are ordered because Q3 depends on Q1, and Q5 depends on the
CloudTrail reader/trail decisions.

**Operational procedure:** `docs/runbooks/first-live-stop-instances.md`

That runbook is **blocked on this record**. It delegates executor location,
mutation principal, forensic reader, trail identity, and the delivery policy here
rather than choosing any of them itself, and it states at the top that no step of
it may be attempted until this record is populated and reviewed. The two are one
unit: this record fixes the environment, the runbook executes against it, and
neither is meaningful alone.

---

## DECISION 1 — Executor environment

**Q1. Where will the mutation executor run?**

```
Executor location:
    OPEN — No executor environment is established. The only host inspected is a
    Windows developer workstation (C:\Users\Sujal, OneDrive\Documents). That is
    a development machine, not a designated execution environment, and it is
    where the only AWS identity in reach is configured.

Executor identity / mechanism:
    OPEN — No execution identity has been designated. See Q2: the sole reachable
    identity is account root and is unsuitable.

AWS account:
    OPEN — 527557823928 is the only account reachable from this host, but it is
    NOT established as the executor account or the target account. It is recorded
    here as the only account observed, not as a decision.

AWS region:
    OPEN — us-east-1 is the region configured in the local AWS profiles. No target
    region has been chosen, and the executor region is not established.

Target instance:
    OPEN — No sacrificial target has been deliberately identified. No candidate
    was selected, deliberately not on the basis that an instance merely exists.

Target InstanceId:
    OPEN — No target instance id has been chosen. See "Target instance" above.

Target region:
    OPEN — Depends on target selection.

Target account:
    OPEN — Depends on target selection.
```

**Required invariant**

The mutation executor is the identity/location authorized to perform the single
`StopInstances` dispatch.

The executor is **not** the CloudTrail forensic reader.

**Decision:** [ ] ACCEPTED  [ ] REJECTED  [ ] OPEN

**Notes:** Q1 is OPEN, and it blocks Q2 by dependency: the role-attachment
mechanism cannot be settled before the executor location is. Two findings from
inspection make this more than a paperwork gap. First, the only reachable
identity is the account **root** user, which cannot be scoped by any IAM policy
and therefore cannot satisfy the one-instance resource invariant in Q2 — so if
this account is to be used, a dedicated scoped role must be created first.
Second, there is no CloudTrail trail in this account (Q4), so the independent
verification this whole milestone exists to provide currently has **no data
source**. Creating a trail is an AWS mutation and is not authorized by this
record.

---

## DECISION 2 — Mutation principal / role attachment

**Q2. Which principal performs the actual `StopInstances` call?**

```
Mutation principal ARN:
    OPEN — No mutation principal exists. The only identity reachable from this
    host is arn:aws:iam::527557823928:root, which is AFFIRMATIVELY REJECTED as a
    mutation principal: the root user cannot be constrained by IAM policy, so it
    cannot be scoped to exactly one target instance ARN. ADR 0009 requires that
    scope, and no amount of care at the console substitutes for it.

Credential acquisition mechanism:
    OPEN — No mechanism is designated. Note the standing M15-E invariant: the
    mutation client takes explicit credentials and refuses profile/ambient
    fallback, so whichever mechanism is chosen must supply credentials explicitly.

Role type:
    [ ] Dedicated execution/task role
    [ ] AssumeRole
    [ ] Other: __________
    OPEN — Not selected. Depends on Q1.
```

**If AssumeRole is selected:**

```
Source principal:
    OPEN

Target role:
    OPEN

Trust relationship verified:
    [ ] YES
    [ ] NO

sts:AssumeRole permitted:
    [ ] YES
    [ ] NO
```

**If a dedicated execution/task role is selected:**

```
Role ARN:
    OPEN — No such role exists in the reachable account.

Role attachment mechanism:
    OPEN — Depends on Q1 executor location (EC2 instance profile, ECS task role,
    or another compute attachment). Not selectable until Q1 is answered.

IAM policy attached:
    OPEN — No policy is attached. The scoped artifact in
    sws_agent.ec2_mutation_client (mutation_iam_policy / validate_iam_policy)
    generates and validates the policy document, but nothing is attached to any
    principal.
```

**Required mutation permission:** `ec2:StopInstances`

**Resource scope:** exactly one target instance ARN

**Required observation permission:** `ec2:DescribeInstances`

**Decision:** [ ] ACCEPTED  [ ] REJECTED  [ ] OPEN

**Notes:** OPEN, blocked by Q1. Two things are settled and should not be reopened:
the permission model is `ec2:StopInstances` plus `ec2:DescribeInstances`, with
`DescribeInstances:*` remaining the accepted read-only overbreadth from ADR 0009
(it is a read, and tightening it was judged not worth the operational risk); and
resource scope must remain exactly one target instance ARN. What is **not**
settled is any principal, and the one available candidate is disqualifying rather
than merely unchosen — account root cannot be scoped at all. Creating a dedicated
role is an AWS mutation and is not authorized by this record, so it is recorded
as a required future step, not as a decision taken here.

---

## DECISION 3 — Forensic reader

**Q3. Does a separate read-only forensic principal exist that can retrieve
CloudTrail evidence covering the target account/region?**

```
Forensic reader principal ARN:
    OPEN — No forensic reader role exists. The only reachable identity is
    arn:aws:iam::527557823928:root, which is unsuitable here for a different
    reason than in Q2: as root it is the account owner, not a constrained
    read-only principal, and using it would also collapse the separation this
    decision exists to guarantee.

Principal type:
    OPEN

Reader is distinct from mutation principal:
    [ ] YES
    [ ] NO
    UNRESOLVED — Cannot be established. There is exactly one reachable identity
    (root). If that single identity were used for both the mutation and the
    CloudTrail read, the two would not be distinct, and the independence ADR
    0011 requires would be nominal only. Separation is therefore NOT in place.

Reader is distinct from mutation executor:
    [ ] YES
    [ ] NO
    UNRESOLVED — Same single-identity problem as above.

Required CloudTrail permission:
    cloudtrail:LookupEvents
    Also required to resolve trail identity before querying:
    cloudtrail:DescribeTrails

Reader account:
    OPEN — 527557823928 is the only reachable account, not established as the
    reader account.

Reader access mechanism:
    [ ] Direct credentials
    [ ] AssumeRole
    [ ] Other: __________
    OPEN — Not selected. Depends on Q1 and on where the runbook operator runs.
```

**If AssumeRole:**

```
Source operator principal:
    OPEN

Target forensic role:
    OPEN — Does not exist.

Trust relationship verified:
    [ ] YES
    [ ] NO

sts:AssumeRole permitted:
    [ ] YES
    [ ] NO
```

**Required invariant**

The mutation executor **must not** also become the CloudTrail forensic reader
merely for convenience.

**Decision:** [ ] ACCEPTED  [ ] REJECTED  [ ] OPEN

**Notes:** OPEN, and blocked by Q1 as well as by its own absence. The intended
shape, restated so it is not lost:

```
operator / runbook environment
        ↓
separate forensic reader role  (read-only, CloudTrail)
        ↓
CloudTrail
```

and explicitly **not** `mutation executor → CloudTrail`.

This separation is currently **absent, not merely unverified**. Establishing it
requires creating at least two distinct principals — a scoped mutation role and a
read-only forensic role — which are AWS mutations this record does not authorize.
Note also that the operator reaching the forensic role is likely to be a person
at a workstation rather than the executor process; that is intended, and it is
why Q1's executor location and Q3's access mechanism are separate questions.

---

## DECISION 4 — CloudTrail region / trail identity

**Q4. Which CloudTrail event source is authoritative for this run?**

```
Target account:
    OPEN — Not established. Depends on target selection.

Target resource region:
    OPEN — Not established.

CloudTrail lookup region:
    OPEN — Not established. us-east-1 is the only region configured locally and
    the only region inspected, but the target region is unchosen, so the lookup
    region cannot be fixed to it yet.

Trail name:
    NONE — Established negative. `aws cloudtrail describe-trails --region
    us-east-1 --profile opencode` returned {"trailList": []}. No trail exists in
    the only reachable account/region.

Trail ARN:
    NONE — No trail exists to name.

Trail home region:
    NONE — No trail exists.

Trail covers target account:
    [ ] YES
    [ ] NO
    [ ] UNKNOWN
    NOT APPLICABLE — There is no trail to cover anything. This is a confirmed
    absence, distinct from "not yet checked".

Trail covers target region:
    [ ] YES
    [ ] NO
    [ ] UNKNOWN
    NOT APPLICABLE — As above.

Lookup region rationale:
    OPEN — Cannot be reasoned about until a target account and region are chosen.
```

**Required invariant**

`LookupEvents` must be performed against the deliberately selected CloudTrail
region/trail context for the target environment.

A zero-result query against an incorrectly selected region/trail **must not** be
interpreted as proof that no AWS-side event exists.

**Decision:** [ ] ACCEPTED  [ ] REJECTED  [ ] OPEN

**Notes:** OPEN, with one hard blocker that is **established rather than
unresolved**: no CloudTrail trail exists in the reachable account. This is the
most consequential finding in the record.

Without a trail there is no CloudTrail event history, so `LookupEvents` can only
ever return zero rows. Every M15-G guarantee — independent AWS-side
verification, AWS-wins-on-disagreement, exact dispatch-count confirmation — would
be satisfied vacuously or not at all. Worse, it would fail in the most dangerous
direction available: with no trail, a lookup returns clean and empty, which
Policy A would eventually mature into a "settled" zero-event conclusion — an
apparently legitimate finding that no dispatch ever happened.

**Required before this decision can be made:** a trail must exist covering the
target account and region, and its identity recorded above. Creating a trail is
an AWS mutation and is **not authorized by this record**. Note that trail choice
also carries a scope decision — a trail records events for its own account and
region, so for a target in another account an organization trail may be required
instead.

---

## DECISION 5 — CloudTrail delivery / zero-result policy

**Q5. What policy applies when CloudTrail is available but the first query
returns zero matching events?**

**Problem being controlled**

CloudTrail delivery is asynchronous. A query performed immediately after dispatch
may legitimately return zero events even though AWS accepted and processed the
request.

Therefore:

```
"available=True + zero events"

MUST NOT automatically mean

"request was not processed by AWS."
```

**Policy A — bounded wait + re-query**

Values below are derived, and the provenance of each is stated because they are
not all the same kind of fact. They remain unapproved: the Decision line is not
checked.

```
Initial query:
    Immediately after settle completes (t0 = end of the §9 settle window).
    Provenance: runbook §9 — poll every 15s to a 600s deadline.

Delivery maturity target:
    15 minutes after t0.
    Provenance: AWS-documented CloudTrail delivery behaviour (most events are
    delivered within roughly 15 minutes). NOT a guarantee, and NOT verifiable in
    this environment — with no trail (Q4) there is nothing to measure against.

Wait interval:
    180 seconds (3 minutes).

Maximum number of additional queries:
    6 additional queries after the initial one (7 total).

Resulting maximum delivery wait:
    6 x 180s = 1080s = 18 minutes after t0.
    This exceeds the 15-minute delivery target by ~20% margin.

Settled / final query:
    The 7th and last permitted attempt, at t0 + 18 minutes. Only this query may
    support a zero-event conclusion. If the event appears at any earlier attempt,
    stop there — maturity is irrelevant once the event is found.

Every attempt recorded as evidence:
    [x] YES — attempt number, timestamp, lookup region, trail, parameters, result.

Only the final/settled query may produce a zero-event conclusion:
    [x] CONFIRMED
```

**Policy B — explicit delivery-age input**

> **Policy B — implementation dependency:** This option is **not currently
> executable without unfreezing implementation**. Selecting it would require
> changes to `CloudTrailEvidence` and corresponding implementation/tests. It must
> therefore not be selected while implementation is frozen.
>
> For contrast, Policy A requires no code and is the currently executable choice.
> See runbook section 12, "Delivery maturity", for the operational procedure and
> for the limits of the control it provides.

```
CloudTrailEvidence records:
    dispatch timestamp:
        NOT PRESENT — no such field exists

    query timestamp:
        NOT PRESENT — no such field exists

    elapsed time:
        NOT PRESENT — no such field exists

    delivery-age threshold:
        NOT PRESENT — no such field exists

Query performed before threshold:
    UNSUPPORTED — cannot be expressed; would require the fields above.

Zero events after threshold:
    UNSUPPORTED — likewise.
```

**§1 precondition-17 correlation window**

```
Window is defined on:
    AWS CloudTrail EventTime — the API-call time as recorded by AWS.
    NOT delivery time and NOT discovery/query time.

Window start:
    The timestamp recorded immediately before StopInstances is called, minus a
    60-second allowance for clock skew between the executor host and AWS.

Window end:
    Settle completion (t0) + 18 minutes (the maximum delivery wait above)
    + 60 seconds skew allowance.

Total window width:
    Approximately 29 minutes plus the pre-dispatch allowance.
```

**Consistency check (required):** the correlation window is **not** narrower than
the maximum delivery-wait period.

```
Maximum delivery wait:      18 minutes
Window extension past t0:   18 minutes + 60s
Result:                     SATISFIED — the window extends at least as far past
                            settle completion as the delivery policy waits.
```

A clarification worth recording, because the naive reading of this check is
wrong. The window is compared against `EventTime`, which is when AWS processed the
call — not when the event became queryable. So delivery lag **cannot** by itself
push a valid event outside the window, and in principle the window need only span
the dispatch itself. The window is nevertheless extended by the full delivery-wait
period because it costs nothing on a purpose-created sacrificial instance and it
removes three ways to get this wrong: correlating on discovery time by mistake,
an executor host whose clock runs fast, and a future change that treats
`EventTime` as a delivery timestamp.

The trade-off, stated rather than hidden: a wider window makes it more likely that
an unrelated legitimate `StopInstances` by another principal against the same
target falls inside it, which the reconciliation reports as `others` and treats as
`AWS_SIDE_DISAGREEMENT`. On a sacrificial instance that nothing else touches, that
is an acceptable false alarm; on a shared instance it would not be.

**Selected policy:** Policy A, with the values above — **proposed, not approved.**

**Required invariants**

CloudTrail availability is not equivalent to event-delivery completeness.

Unavailability is never inferred as absence.

An early zero-result query cannot independently establish that AWS did not
process the dispatch.

**Decision:** [ ] ACCEPTED  [ ] REJECTED  [ ] OPEN

**Notes:** OPEN on two independent grounds. The operator has not approved these
values, and they cannot be exercised regardless: with no trail (Q4) every query
returns zero rows, so the policy has nothing to wait for.

Policy A remains the only executable option, and it stays procedural. The module
does not enforce maturity — `CloudTrailEvidence` has no query-timestamp field — so
if the wait-and-re-query sequence is skipped, `reconcile_dispatch` will treat an
early zero-result query as `AWS_SIDE_DISAGREEMENT`, manufacturing an incident from
a healthy run. The control is the operator following section 12, not the code.

---

## Dependency graph

```
Q1 Executor environment
        |
        +----> Q2 Mutation principal
        |
        +----> Q3 Separate forensic reader
                    |
                    +----> Q4 CloudTrail region/trail identity
                                |
                                +----> Q5 Delivery policy
```

The decisions are therefore **not** five independent questions.

Minimum ordering:

```
Q1
 |
 +--> Q2
 |
 +--> Q3 --> Q4 --> Q5
```

---

## First-live run gate

The first live `STOP_RESOURCE` execution remains **BLOCKED** unless all of the
following are true:

```
[ ] Executor location is fixed.                        OPEN (Q1)
[ ] Mutation principal is fixed.                      OPEN (Q2)
[ ] Mutation principal is independently verified.     BLOCKED — no principal exists
[ ] Target account is fixed.                          OPEN (Q1)
[ ] Target region is fixed.                           OPEN (Q1)
[ ] Sacrificial target instance is fixed.             OPEN (Q1)
[ ] Target pre-stop state is verified.                BLOCKED — no target
[ ] Dedicated forensic reader exists.                 OPEN (Q3) — none exists
[ ] Forensic reader is separate from mutation executor.
                                                       NOT IN PLACE — one identity only
[ ] Forensic reader access is verified.               BLOCKED — no reader exists
[ ] CloudTrail region/trail identity is fixed.        OPEN (Q4)
[ ] CloudTrail coverage is verified.                  NO — no trail exists
[ ] Delivery policy is selected and operationally executable.
                                                       PROPOSED, NOT APPROVED (Q5);
                                                       not executable without a trail
[ ] DryRunOperation succeeds.                         NOT ATTEMPTED
[ ] Pre-dispatch preflight succeeds.                  NOT ATTEMPTED
[ ] Exactly one mutation dispatch is structurally permitted.
                                                       NOT ATTEMPTED
[ ] Local dispatch witness is captured.               NOT ATTEMPTED
[ ] Settle completes.                                 NOT ATTEMPTED
[ ] Post-state is evaluated separately from CloudTrail reconciliation.
                                                       NOT ATTEMPTED
[ ] CloudTrail evidence is retrieved independently.  IMPOSSIBLE — no trail
[ ] CloudTrail evidence is correlated using the approved attributes.
                                                       NOT ATTEMPTED
[ ] Local witness and AWS-side evidence are reconciled.
                                                       NOT ATTEMPTED
[ ] Any disagreement is handled according to the approved precedence.
                                                       NOT ATTEMPTED
[ ] No second dispatch occurs unless the reconciliation result explicitly
    permits retry.                                   NOT ATTEMPTED
[ ] Operator explicitly authorizes the live mutation.
```

---

## Semantic separation — non-negotiable

**Post-state answers:** "Did the target reach the desired EC2 state?"

Only `STOPPED` is success.

**CloudTrail answers:** "What AWS-side dispatch event(s) can independently be
established?"

CloudTrail is **not** the post-state success check.

**Reconciliation answers:** "Do the independent execution-side and AWS-side
evidence agree?"

It does **not** itself query AWS.

**Unavailable evidence means:** `UNKNOWN` / `UNRESOLVED`

It does **not** mean: `NO DISPATCH`

---

## Current status

```
M15-G:                 COMPLETE (accepted)
Environment record:    POPULATED — 2 hard blockers, 3 decisions OPEN
Implementation:        FROZEN
HEAD:                 39fac2c
Working tree:         Uncommitted M15 work remains as previously reported.
                      No code or test changes authorized by this record.
Live mutation:        NOT AUTHORIZED
AWS mutations:        NONE. Two read-only calls only:
                      sts get-caller-identity, cloudtrail describe-trails
```

### Blockers

Three things must change in AWS before this record can be completed. All three
are AWS mutations and none is authorized here.

1. **No CloudTrail trail exists** (Q4). Without one there is no AWS-side evidence,
   and Policy A would mature an empty result into a false "no dispatch" finding.
   A trail covering the target account and region is a precondition for the entire
   M15-G verification architecture.
2. **The only reachable identity is account root** (`arn:aws:iam::527557823928:root`).
   It cannot be scoped to one instance ARN, so it can never be the mutation
   principal (Q2), and using it as the forensic reader would collapse the
   independence Q3 requires. A dedicated scoped mutation role and a separate
   read-only forensic role are both needed.
3. **No executor environment is designated** (Q1), and therefore no target
   instance, account, or region. Q2 and Q3 both depend on it.

**Next action:** decide the executor environment (Q1). Everything else follows
from it, and no runbook step may be attempted until Q1 through Q5 are resolved
and the live mutation is separately authorized.