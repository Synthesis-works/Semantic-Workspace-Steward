# M15 — Environment Decision Record

**Status: DRAFT — NO LIVE MUTATION AUTHORIZED — ENVIRONMENT BLOCKED**

- HEAD at record creation: `39fac2c`; M15-A through M15-G checkpoint: `5e4215f`
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
| `aws sts get-caller-identity --profile opencode` (read-only) | Account `527557823928`, identity **`arn:aws:iam::527557823928:root`**. Re-confirmed during the Q1 investigation. |
| `aws cloudtrail describe-trails --region us-east-1` (read-only) | `{"trailList": []}` — **no trail exists** in this account/region. |
| `.github\workflows\ci.yml` (Q1 investigation) | The **only** workflow. `runs-on: ubuntu-latest`, GitHub-hosted and ephemeral. No `self-hosted`, no `aws-actions/configure-aws-credentials`, no OIDC/trust reference, no role ARN. States the suite is deliberately hermetic and that adding the AWS extra would invite the live-AWS validation the suite avoids. |
| Repository scan for `self-hosted` / `oidc` / `role_arn` / `assume-role` | No CI or infrastructure occurrence. The only `assume-role` reference is a **placeholder** in the runbook (`--role-arn <mutating-role-arn>`), and all `amazonaws.com` matches are synthetic fixtures or the expected `eventSource`. |
| Local AWS config keys (Q1 investigation) | Profiles `opencode` and `default` carry **only** `region` and `login_session`. No `role_arn`, no `source_profile`, no `credential_process`. No configured assume-role path. |
| `aws --version`, host/OS, `python --version` (Q1 investigation) | `aws-cli/2.36.31`; `DESKTOP-4FC9G4O`, Windows 11 **Home** Single Language build 26200; Python 3.14.3; `sws_agent` importable from `D:\SWS\src`. |
| `AWS_*` environment variable **names** | **None set.** Names only were inspected; no variable values were read or printed. |

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

Investigated 2026-10-05, Q1 only. Candidates are classified
**A** verified / **B** exists but unverified / **C** configured but not available /
**D** hypothetical / **E** none.

```
Executor location:
    OPEN — No executor environment is established. Four candidates were
    investigated; none is an available, designated executor. See the candidate
    table below for each one's classification and the evidence behind it.

Executor type:
    OPEN — undetermined. It follows from the location decision, which is not
    made. Not assumed to be a workstation, a runner, or a host.

Operating system / runtime:
    OBSERVED, NOT DECIDED — the only inspected host runs
    Microsoft Windows 11 Home Single Language, build 26200, hostname
    DESKTOP-4FC9G4O, with Python 3.14.3 and aws-cli/2.36.31 present.
    sws_agent is importable from D:\SWS\src. This records that the machine
    *could* run the procedure; it does not designate it as the runner.

AWS account:
    OPEN — 527557823928 is the only account reachable from the inspected host,
    but it is NOT established as the executor account or the target account. It
    is recorded here as the only account observed, not as a decision.

AWS region:
    OPEN — us-east-1 is the region configured in the local AWS profiles. No target
    region has been chosen, and the executor region is not established.

AWS identity / credential mechanism:
    OPEN — no execution identity is designated. The only configured path is
    `login_session = arn:aws:iam::527557823928:root` in the local AWS config,
    confirmed by a read-only `sts get-caller-identity`. No `role_arn` and no
    `source_profile` are present, so there is no configured assume-role path.
    See Q2 for why root is unsuitable.

Identity currently usable:
    OPEN — not a usable *mutation* identity. The root path is reachable
    (`sts get-caller-identity` succeeds, account 527557823928), but reachability
    is not suitability, and suitability is Q2.

Can it perform the read-only preparation steps?
    YES for this host only — the AWS CLI and the SDK import resolve, and the
    read-only preflight calls the runbook needs are callable from it. This is
    capability of a development machine, not evidence of an approved executor.

Can it perform the exact mutation path if separately authorized?
    UNCONFIRMED — no scoped identity exists to authorize, so the mutation path is
    not merely unauthorized but undefined. Settling it is Q2, not Q1.

Environment distinct/controllable enough for a first-live run:
    NO — not established. The one host that can reach AWS is a personal Windows
    *Home* workstation, with the AWS session and the working tree on the same
    account and no separation between the operator's environment and the
    executor's.

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

### Q1 candidate table

| # | Candidate | Class | Evidence | Why it is not an executor today |
| --- | --- | --- | --- | --- |
| 1 | Local workstation `DESKTOP-4FC9G4O` | **B** — exists, unverified | Windows 11 Home build 26200; `aws-cli/2.36.31` at `%LOCALAPPDATA%\Programs\Amazon\AWSCLIV2\aws.exe`; Python 3.14.3; `sws_agent` importable; read-only `sts get-caller-identity` returns `arn:aws:iam::527557823928:root` | It is a personal development machine, not a designated execution environment. Root cannot be scoped (Q2), and root is the only path it has. Nothing distinguishes the operator's environment from the executor's. |
| 2 | GitHub Actions CI | **C** — configured, not available | `.github/workflows/ci.yml` is the only workflow. `runs-on: ubuntu-latest` (GitHub-hosted, ephemeral). No `self-hosted`, no `aws-actions/configure-aws-credentials`, no OIDC/trust reference, no role ARN, no AWS env vars. The workflow documents that the suite is **deliberately hermetic** and that installing the AWS extra would "invite the live-AWS validation the suite is built to avoid". | CI has **no AWS credentials of any kind** by design. It cannot reach AWS, and using it as the executor would mean deliberately adding credentials to a pipeline that was built specifically to stay away from live AWS. |
| 3 | Self-hosted runner | **E** — none | No `self-hosted` label, runner registration, or runner documentation anywhere in the repository or local config | No such runner is configured. Nothing to verify. |
| 4 | Dedicated host / separate account | **E** — none | No Terraform, CloudFormation, CDK, SAM, Pulumi, Ansible, or Docker configuration exists in the repository. `~/.aws\config` contains two profiles (`opencode`, `default`) that differ only in name and both point at the same account and region. | No infrastructure is defined anywhere, so no dedicated host or second account is known to exist. |

Strongest candidate is **#1**, and it is classified **B** rather than **A**
deliberately: the host demonstrably exists and can reach AWS, but nothing about it
is *verified as an acceptable executor*. Technical capability is not
designation, and the two findings below are disqualifying on their own rather
than merely unproven.

**Required invariant**

The mutation executor is the identity/location authorized to perform the single
`StopInstances` dispatch.

The executor is **not** the CloudTrail forensic reader.

**Decision:** [ ] ACCEPTED  [ ] REJECTED  [ ] OPEN

> **Q1 is OPEN WITH A DEFINED TARGET ARCHITECTURE.** Architecture **A** (a
> dedicated AWS execution environment in the target account, with the mutation
> role attached directly and the forensic reader carried separately) is
> recommended as the target. That is a target, not an executor: nothing is
> provisioned, so Q1 stays OPEN. Full comparison, criteria, scoring and the
> minimum provisioning package are in **"Q1 — Executor architecture decision"**
> below. The single smallest operator choice still outstanding is recorded in
> that section.

**Notes:** Q1 is OPEN. Four candidates were investigated and the strongest is
classified **B — exists but not verified**, not resolved: the workstation is
real and can reach AWS, but it is a personal Windows *Home* development machine,
not a designated execution environment, and no evidence separates the operator's
environment from the executor's. The only AWS-capable environment is therefore
also the one least suited to hold the blast radius of a first live mutation.

Q1 blocks Q2 by dependency: the role-attachment mechanism cannot be settled
before the executor location is, because there is nowhere known to attach it. The
supporting facts are these. The only reachable identity is the account **root**
user, which no IAM policy can scope, so it cannot satisfy the one-instance
resource invariant in Q2 — a dedicated scoped role must exist first. That path
does not exist yet: the local AWS config declares no `role_arn` and no
`source_profile`, so there is no configured assume-role mechanism at all, and
enumerating IAM principals to find one is a broad discovery scan that is out of
scope and would not change the conclusion. GitHub Actions is the only other
candidate and holds no AWS credentials whatsoever, by deliberate design. There is
no CloudTrail trail in this account (Q4), so the independent verification this
milestone exists to provide currently has **no data source**; creating one is an
AWS mutation this record does not authorize.

Two of these are established *negative* facts rather than gaps in investigation:
no assume-role path is configured, and CI carries no AWS credentials. Resolving
Q1 therefore requires an operator decision — designate an executor environment
(or provision one) — not more inspection.

---

## Q1 — Executor architecture decision

This subsection is an **architecture** decision, not a provisioning one. It
answers "which executor architecture should SWS use for the first live stop?" It
does **not** answer "does an executor exist?" — nothing has been provisioned, so
Q1 remains **OPEN**, now as **OPEN WITH A DEFINED TARGET ARCHITECTURE**.

### Current state

No executor is designated. No executor is operational. Nothing was created while
writing this subsection, and the four candidate environments investigated above
are described as they are *today*, not as they could become.

### Decision criteria

Every criterion below is extracted from the existing SWS design. None is
invented for this comparison.

| # | Criterion | Source in the existing design |
| --- | --- | --- |
| C1 | Isolation from ordinary development activity | Runbook header, **Exposure**: the mutation stays "private and local"; ADR 0010 items 1–4 defer executor location deliberately |
| C2 | Credential containment | Runbook §3: the client takes its credential **explicitly**, "no fallback to an environment variable, a shared profile, or an instance-profile role"; ADR 0009 credential policy |
| C3 | Ability to carry a least-privilege, single-instance mutation identity | Precondition 9 and precondition 14; ADR 0009 IAM section |
| C4 | Ability to carry a **separate** read-only forensic identity | Preconditions 10 and 16; ADR 0011 — the reconciliation refuses evidence whose reader is the mutating principal |
| C5 | Explicit account and region control | Runbook §2 (refuse on account mismatch) and §4 (exactly one reservation); precondition 18 |
| C6 | Operator control and auditability | Precondition 11 (named human, in writing, instance id included); runbook §6 (approver is not the operator); §11 evidence retained |
| C7 | Reproducibility | Record 0012 as a whole; runbook §1 (eighteen preconditions recorded pass/fail with evidence) and §12.0 (window recorded in advance, "not chosen afterwards") |
| C8 | Ability to execute the private/local runbook as written | Runbook header **Exposure**; §3, which requires that the role "can be assumed by anything other than the private CLI's principal" — it may **only** be that |
| C9 | Ability to perform the CloudTrail reconciliation step | Runbook §12 and §12.1; precondition 18 (lookup region and trail identity fixed in advance) |
| C10 | Blast-radius containment | Runbook §5 header, scope (one purpose-created sacrificial instance); preconditions 1–7 |
| C11 | Ease of proving the executor's identity | Runbook §2 (record `Account`, `Arn`, `UserId`), §3 (record `AssumedRoleUser.Arn`, expiry, **session name** as the ledger↔CloudTrail join key) |
| C12 | Risk of accidental reuse for ordinary development | ADR 0010 MCP pin (`EXPECTED_MCP_TOOL_NAMES`, nine tools); `implemented` must stay empty; runbook header (mutation unreachable through MCP) |
| C13 | Provisioning complexity | SWS design preference, ADR 0010: avoid "a second mutable permission boundary" |
| C14 | Ongoing operational complexity | Same ADR 0010 preference; the run must remain re-runnable for later validations |

### Comparison matrix

Scale: **5** strongly satisfies · **4** satisfies with minor caveat · **3** workable
with a meaningful weakness · **2** poor fit · **1** fundamentally conflicts with
first-live safety.

Scores are deliberately coarse. A score of **T** means *target architecture,
conditional on provisioning that does not exist yet*; a score of **S** means
*score is for the current state of that candidate, as it exists today*. The
distinction is the point: **B** and **C** are not available now, and their scores
must not be read as if they were.

| # | Criterion | A. Dedicated AWS env | B. Existing external host | C. GitHub Actions | D. Windows workstation |
| --- | --- | --- | --- | --- | --- |
| C1 | Isolation from development | 5 T | **N/A** | 4 T | **1 S** |
| C2 | Credential containment | 5 T | **N/A** | 4 T | **1 S** |
| C3 | Least-privilege mutation identity | 5 T | **N/A** | 5 T | **1 S** |
| C4 | Separate forensic identity | 4 T | **N/A** | 4 T | **1 S** |
| C5 | Explicit account/region control | 5 T | **N/A** | 5 T | 3 S |
| C6 | Operator control, auditability | 4 T | **N/A** | 3 T | 2 S |
| C7 | Reproducibility | 4 T | **N/A** | 3 T | 2 S |
| C8 | Executes the runbook as written | 4 T | **N/A** | **2 T** | 5 S |
| C9 | CloudTrail reconciliation | 5 T | **N/A** | 5 T | 4 S |
| C10 | Blast-radius containment | 5 T | **N/A** | 4 T | **1 S** |
| C11 | Proving executor identity | 5 T | **N/A** | 4 T | 2 S |
| C12 | Accidental development reuse | 5 T | **N/A** | **2 T** | **1 S** |
| C13 | Provisioning complexity (5 = simplest) | 2 T | **N/A** | 3 T | 5 S |
| C14 | Ongoing complexity (5 = simplest) | 2 T | **N/A** | 3 T | 5 S |

**B — NO EXISTING CANDIDATE FOUND.** The previous investigation established that no
controlled external host, self-hosted runner, dedicated host, or separate AWS
account exists in evidence. This option is therefore **not scored rather than
scored poorly**, and it is **not** recommended. Promoting it would mean inventing
a machine and then treating the invention as a decision.

### Architecture detail

**A. Dedicated AWS execution environment** — a host whose sole purpose is to
execute this procedure.

- *Would provide:* genuine isolation from development activity (C1), credentials
  attached by role rather than stored on a personal machine (C2), a dedicated
  principal that carries only `ec2:StopInstances` plus the ratified
  `DescribeInstances` overbreadth on exactly one instance ARN (C3), an explicit
  `Arn` to record at runbook §2 and §3 (C11), and a blast radius bounded by that
  policy (C10).
- *Safety properties it uniquely satisfies:* it is the only candidate where the
  executor's identity is **independent of any human's workstation**, which is what
  makes "the approver is not the operator" (§6) and precondition 11 auditable
  rather than self-asserted. It is also the only candidate where ADR 0010's stated
  preference can actually be applied — a task role or instance profile gives
  `environment → role → EC2` instead of `environment → STS AssumeRole → role →
  EC2`, removing one mutable permission boundary (C13).
- *Still to be provisioned:* the host itself, its access path, the mutation role
  and policy, the attachment mechanism, the separate forensic reader, the
  CloudTrail trail, and the sacrificial target.
- *Residual risks:* a dedicated host is itself an attack surface and a cost
  commitment; access to it becomes a control in its own right; a host in the
  wrong region or account silently breaks the §2/§4 preconditions.
- *Appropriateness:* yes, for the first live run. It is the highest-effort option
  and that effort is the safety property being bought.

**C. GitHub Actions ephemeral executor with a provisioned AWS identity** — real
architectural potential, and materially better than D on most axes.

- *Would provide:* ephemeral isolation per job (C1), short-lived OIDC credentials
  with no stored keys (C2), a policy-boundable identity (C3), explicit account and
  region in the job definition (C5), and retention via artifacts (C6).
- *What would have to change:* a GitHub OIDC provider and trust relationship, an
  `aws-actions/configure-aws-credentials` step, a second role for the forensic
  read, environment-scoped approvals, and a **new** workflow — `ci.yml` must not
  be modified for this. None of this exists.
- *Two findings that hold it below A.* First, runbook §3 permits the mutation role
  to be assumable by **"the private CLI's principal"** and nothing else. A
  GitHub-hosted runner's OIDC subject is a different principal class, so using C
  would require **amending the runbook's own precondition** rather than merely
  satisfying it — the first live run should not begin by relaxing a written
  control. Second, C carries the **worst** accidental-reuse profile of any
  candidate: `ci.yml` runs on every push to `main`, so placing a mutation path in
  that repository puts it in the same trigger surface as ordinary development.
  Ephemeral runners also remove durable state, and §11 expects retained evidence.
- *Verdict:* viable for a later automation milestone; not recommended for the
  first live stop.

**D. Current Windows developer workstation** — *unsuitable in its current state*,
which is not the same as *never usable*.

- *Current state (S scores):* Windows 11 **Home** on a personal machine, holding
  the account root identity and the working tree together. C1, C2, C3, C4, C10 and
  C12 score 1 because the same environment that runs `pytest` also holds an
  unscopable identity capable of stopping anything in the account. C12 is the
  sharpest: ADR 0010 pins the MCP surface to nine tools and holds `implemented`
  empty precisely so the mutation cannot be reached by accident, and a
  general-purpose dev workstation is the least controlled place to make one
  `StopInstances` call.
- *Could it be made safe?* **Partly, and not enough.** Provisioning a properly
  scoped role and a separate forensic reader would repair C2, C3 and C4. It would
  **not** repair C1, C10 or C12: the host would still be a personal development
  machine with a full checkout of the repository on it. The architecture can
  therefore become *less* wrong with provisioning, but it never becomes the
  isolated executor the first live run is supposed to demonstrate.
- *One genuine advantage, recorded for honesty:* D is the only candidate that
  satisfies C8 today — it *is* the private local CLI the runbook was written for.

### Recommended architecture

**A — a dedicated AWS execution environment, in the target account, with the
mutation role attached directly (instance profile / task role) and the forensic
reader carried separately.**

Ranked by the stated priority, SAFE FIRST LIVE > REPRODUCIBILITY > AUDITABILITY >
OPERATIONAL SIMPLICITY > SPEED, this is the recommendation.

*Why it wins.* It is the only candidate that satisfies C1, C2, C3, C10 and C12
simultaneously — and C1 and C10 are precisely what the first live run is meant to
exercise. D fails them today and cannot fully repair them by provisioning. C
scores well on credentials but fails C8 as the runbook is currently written, and
scores worst of all on C12. D wins C8 and C13/C14 outright; those are explicitly
the lowest-weighted criteria, and paying for them with isolation is the wrong
trade for a first irreversible action.

*What safety boundary it establishes.* The executor becomes an AWS principal whose
capability is a policy over exactly one instance ARN, reached from a host whose
only job is this run, with no human workstation standing between the operator and
an unscopable identity. Evidence produced by the run is attributable to that
principal and not to a person.

*What it lets Q2 become.* Q2 stops being "find an attachment mechanism in the
abstract" and becomes concrete and checkable: attach the scoped mutation role to
the dedicated executor, or record why direct attachment is unavailable and use
`sts:AssumeRole` — which is exactly the decision rule ADR 0010 already wrote down.
Q2's one-instance-scope invariant becomes enforceable against a known principal
rather than an unknown environment.

*One tension the operator must settle, not this record.* ADR 0010 prefers direct
attachment, but runbook §3 currently assumes `sts:AssumeRole` with an
`--external-id`. Choosing A **with an instance profile** therefore requires a
small, explicit runbook amendment to §3 before the run. That amendment is
recorded here as a prerequisite rather than made unilaterally, because relaxing
a written credential control is an operator decision.

### Minimum provisioning package

**ALREADY EXISTS**

- The SWS implementation and its 1396-test suite, including `preflight`,
  `mutation_evidence`, `ec2_mutation_client` and `reconciliation`.
- The written procedure (`first-live-stop-instances.md`, 18 preconditions).
- One AWS account and region reachable from this workstation, and a confirmed
  root identity — usable for **provisioning and verification only**, never as the
  mutation principal.

**MUST BE PROVISIONED (none of it exists; none authorized)**

- A dedicated execution host, with an access path the operator controls.
- The mutation role and a policy scoped to exactly one instance ARN, validated by
  `validate_iam_policy`.
- The chosen attachment: instance profile / task role, or an `AssumeRole` trust
  whose principal is restricted per runbook §3.
- A **separate** read-only forensic role able to call `cloudtrail:LookupEvents`,
  with the mutating role confirmed not to hold it.
- A CloudTrail trail covering the target account and region, with its lookup
  region and trail identity fixed in Q4 before any dispatch.

**OPERATOR DECISION REQUIRED**

- Approve architecture A, and choose **same account** versus a separate executor
  account.
- Choose the attachment mechanism, which selects whether runbook §3 needs the
  amendment described above.
- Supply or designate the sacrificial instance and confirm all 18 preconditions.

**NOT YET NEEDED**

- GitHub OIDC provider, trust relationship, or any change to `ci.yml`.
- A self-hosted runner.
- Any change to the MCP surface (still nine tools) or to `implemented` (still
  empty).
- Any application code or test change.

### Explicit boundary

- **Q1 is OPEN** — as *OPEN WITH A DEFINED TARGET ARCHITECTURE*. Architecture
  selected is **not** executor exists.
- **Q2, Q3, Q4 and Q5 remain OPEN** and were not examined in this subsection.
- No provisioning occurred. No AWS resource was created, modified or deleted.
- Operator authorization is **not** granted by this subsection. The live gate in
  "First-live run gate" remains closed and its authorization box unchecked.

---

## Q1 — Identity and credential architecture

This subsection resolves the complete identity chain behind architecture A, so
that Q1 through Q4 can be provisioned coherently in one later phase. It is a
**design** step: nothing below was created, and every arrow is labelled with what
exists today.

### The complete identity chain

```text
  HUMAN OPERATOR                                      [ARCHITECTURAL DECISION]
  (named, in writing, precondition 11)                NOT YET DECIDED
        |
        | controlled access                            [MUST BE PROVISIONED]
        | (no mechanism chosen; see Operator access)
        v
  DEDICATED EXECUTOR ENV                              [MUST BE PROVISIONED]
  in the target account, sole purpose = this run      does not exist
        |
        | DIRECT ATTACHMENT — no second trust hop     [DECISION, this section]
        | credentials sourced from the instance profile
        v
  MUTATION IAM ROLE                                   [MUST BE PROVISIONED]
  ec2:StopInstances  -> exactly one instance ARN      does not exist
  ec2:DescribeInstances -> *  (ratified M15-E)
  NO CloudTrail read, NO iam:*, NO sts:*              does not exist
        |
        | EC2 permission boundary
        v
  SACRIFICIAL INSTANCE                                [NOT YET SELECTED]
  no instance chosen; target selection is Q1's
  remaining OPEN content
```

The forensic path is separate and never joins the mutation path above:

```text
  FORENSIC READER ROLE                                [MUST BE PROVISIONED]
  cloudtrail:LookupEvents                             does not exist
  NO ec2:StopInstances, NO ec2:*, NO mutation
        |
        | CloudTrail read                              [MUST BE PROVISIONED]
        v
  CLOUDTRAIL TRAIL                                    [MUST BE PROVISIONED]
  covering target account + region                    does not exist (Q4)
        |
        | LookupEvents -> CloudTrailEvidence
        v
  RECONCILIATION  (sws_agent.reconciliation)          [EXISTS NOW]
  require_reconciliation; rejects reader == mutator   45 hermetic tests
```

### Every arrow, answered

| Arrow | Principal | Credential mechanism | Boundary location | Can mutate? | Can read CloudTrail? | Distinct? | Independently verifiable? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Operator → Executor | human operator | **NOT YET DECIDED** — no access mechanism selected | at the access mechanism | no | no | yes, if access is not the mutation role | yes, via host/SSM session logs |
| Executor → Mutation role | **MUST BE PROVISIONED** | instance-profile credentials, passed **explicitly** to the client | the instance profile | **yes** | **no, by design** | yes | yes, `sts:GetCallerIdentity` at runbook §2 |
| Mutation role → EC2 | mutation role | same credentials | the IAM policy resource ARN | yes, one instance | no | n/a | yes, `validate_iam_policy` + precondition 14 |
| Forensic reader → CloudTrail | **MUST BE PROVISIONED** | separate credentials, separate source | its own policy | **no** | yes, `LookupEvents` only | **yes — required by ADR 0011** | yes, recorded in `CloudTrailEvidence.reader_principal_arn` |
| CloudTrail → Reconciliation | reader principal | data only | the module boundary | no | reads only | yes | yes, `require_reconciliation` |

**EXISTS NOW:** the reconciliation module and its provenance check; the IAM policy
*artifact* and its validator; the client factory's explicit-credentials contract;
the runbook. **MUST BE PROVISIONED:** every AWS principal and host in the two
diagrams above. **NOT YET DECIDED:** operator access mechanism, account boundary.
**NOT YET SELECTED:** the sacrificial instance.

Nothing in the chain is inferred from the requirement that it exist. Every AWS
principal in these diagrams is currently absent.

### Credential-model conflict: resolved

Runbook §3 currently instructs `sts:AssumeRole --role-arn <mutating-role-arn>
--external-id <external-id>`, while architecture A recommends direct attachment.
The two were compared on fifteen axes.

| Axis | Model A — direct instance profile | Model B — `sts:AssumeRole` |
| --- | --- | --- |
| Least privilege | Equal — both end at the same policy | Equal |
| Credential containment | **Better.** One principal, no second role to trust | Weaker — the initial identity can also assume other roles |
| Secret exposure | **Better.** No long-lived key anywhere; nothing to leak at rest | Weaker — an initial credential exists on the host or workstation |
| Proving executing identity | **Better.** `Arn` is the role itself | Weaker — identity is `role/session`, needs the session name joined |
| Separation from dev credentials | **Better.** Host creds are not developer creds | Weaker — the initial identity is often a human's |
| External-ID usefulness | **Not applicable** — there is no assume step | Its main use. Useful here, **but see the finding below** |
| Operator control | **Better.** Access to the host *is* the control | Weaker — holding the initial credential permits the assumption |
| Blast radius | **Better.** One hop from environment to action | Weaker — the assume step is itself a permission to grant |
| Reproducibility | **Better.** Host profile is declarative | Weaker — depends on a caller presenting the right external id |
| ADR 0009 compatibility | Compatible — §2 requires explicit credentials, which the client still receives | Compatible |
| ADR 0010 compatibility | **Preferred by ADR 0010's own decision rule** | Permitted only "when no direct attachment is available" |
| Runbook §3 compatibility | **Conflict — §3 amendment required** | As written |
| Complexity | Lower — no trust policy to get wrong | Higher — trust policy, external id, session naming |
| Failure modes | Profile missing/wrong role — fails loudly, preflight | Trust misconfigured → confusing auth error; wrong external id → silent refusal |
| First-live suitability | **Yes** | Acceptable fallback |

**Recommendation: Model A, direct instance-profile attachment.** ADR 0010 already
wrote this decision rule down — "if that environment supports a task role,
instance profile, or equivalent direct attachment, attach the dedicated mutation
role there. Use `sts:AssumeRole` only when no direct attachment is available, and
record why" — and Model B violates it unless direct attachment is *unavailable*,
which for a dedicated EC2 host it is not. Model A also removes one mutable
permission boundary, which is the preference ADR 0010 states explicitly.

**The additional STS boundary is not desirable here.** It does not add safety to
the first live stop; it adds a second grantable permission. The external ID
protects against *confused-deputy* assumption, which is a real concern when a
third party triggers an assumption. That concern does not apply when the role is
attached to a host dedicated to this one run: there is no other party able to
trigger it.

**Runbook §3 therefore requires a controlled amendment before provisioning**, and
one has been made below. The amendment preserves the section's actual purpose and
both of its non-obvious checks.

### Amended runbook §3

The original §3 is preserved in place and annotated rather than rewritten, so the
change is reviewable and the prior instruction is not silently erased.

Two things this amendment deliberately does **not** do: it does not relax "do not
proceed if the role can be assumed by anything other than the private CLI's
principal" — under Model A there is no assume path at all, which satisfies that
constraint more strictly than AssumeRole could; and it does not weaken the
session-expiry or session-name checks, which remain required because both bear on
settle-window correctness and on the ledger↔CloudTrail join.

### Mutation identity design

**Not created. Defined only.**

| Property | Value | Basis |
| --- | --- | --- |
| Type | IAM **role**, not a user | no long-lived key exists to leak; ADR 0009 §2 |
| Attached to | the dedicated executor's instance profile | Model A above |
| Trust policy | no trust relationship needed for a directly attached role | Model A |
| `ec2:StopInstances` | `Resource` = **exactly one** instance ARN | ADR 0009 §6, precondition 9 |
| `ec2:DescribeInstances` | `Resource` = `*`, read-only | **RATIFIED M15-E**, unchanged |
| CloudTrail read | **Prohibited** | precondition 16; ADR 0011 |
| `iam:*` | **Prohibited** | blast radius |
| `sts:*` | **Prohibited** — including `sts:AssumeRole` | Model A removes the need; granting it would reopen the second boundary |
| Any other EC2 action | **Prohibited** — no terminate, reboot, modify, start | ADR 0009 §6; `validate_iam_policy` refuses any other action |
| Wildcard actions | **Refused** | `_ALLOWED_ACTIONS` is exactly the two above |
| Account boundary | the target account only | ADR 0009 §6 |
| Region | no IAM region condition exists; the client's explicit region bounds it | `MutationClientSettings.region`, ADR 0009 §2 |

`DescribeInstances: *` is **not** broadened here and is not re-litigated: it is
owner-ratified at M15-E, it is read-only, and removing it would make post-dispatch
state unclassifiable. It remains confined to this one role and must not be copied
into any future role.

**How the identity will be proven at run time:** `sts:GetCallerIdentity` at §2
records `Account`/`Arn`/`UserId` and must match the expected role ARN exactly;
precondition 14 re-validates the policy against the one recorded instance ARN;
`effective_retries` proves the single-attempt configuration at dispatch. The
`Arn` at §2 is the direct evidence that the executing principal was the intended
role.

### Forensic reader design

**Not created. Defined only.**

| Property | Value | Basis |
| --- | --- | --- |
| Type | IAM role, separate from the mutation role | ADR 0011 |
| `cloudtrail:LookupEvents` | permitted | required for reconciliation |
| Trail-identity API | `cloudtrail:DescribeTrails` permitted | Q4 needs trail identity fixed first |
| `ec2:StopInstances` | **Prohibited** | precondition 16 |
| Any `ec2:*` | **Prohibited** | it must not be a second mutation path |
| `iam:*` | **Prohibited** | blast radius |
| Where it runs | the executor, using a **second, distinct** credential source | ADR 0011 |

**Why separate:** "the credential that can change things must not be able to
inspect itself." A witness plus a self-read is one actor's account of itself.
`reconciliation.py` enforces this mechanically — `CloudTrailEvidence.reader_principal_arn`
must not equal the mutating principal, and evidence failing that check is refused
rather than trusted. Separation is therefore a code-enforced invariant, not a
convention.

**How its observations become independent evidence:** the reader's principal ARN is
recorded on the evidence, the module compares principal + session + target + window,
and `require_reconciliation` raises unless the outcome is `CONFIRMED`. Where the
witness and CloudTrail disagree, AWS is the fact.

**How it avoids becoming a second mutation boundary:** it holds no EC2 permission at
all, so it cannot stop anything.

### Operator access model

**NOT YET DECIDED.** Recorded here as a requirement rather than a mechanism.

The controlling invariant is `operator ≠ mutation role`. Concretely: the operator
reaches the host through a controlled mechanism; the host's instance profile
supplies the mutation role; no operator-held credential can assume the mutation
role. Holding operator access must not confer mutation capability, or the
separation the architecture exists to create would be cosmetic.

Access via an AWS-controlled session channel (Systems Manager Session Manager and
its AWS API are the realistic options) is **architecture potential, not a
selection**, and none is provisioned or chosen by this record.

Whether the operator's access should itself be distinct from the forensic reader is
recorded as a **further desirable property, not a decided one**. The first-live
requirement is that the forensic reader be distinct from the *mutator*, which
precondition 16 and the module enforce. Making the operator's access distinct from
the forensic reader as well is stronger than required and is left to the operator.

**Auditable operator action:** the approval in precondition 11 is a named human's
written authorization naming the instance id; runbook §6 requires the approver to
be a human and **not** the operator, and consumes an already-existing approval
rather than creating one. Combined with §2 recording the executing `Arn`, the run
attributes the action to a specific AWS principal and a specific authorizing human.

### Account boundary

| Axis | Option 1 — same account `527557823928` | Option 2 — separate executor account |
| --- | --- | --- |
| Isolation | Weaker — executor and target share an account boundary | Stronger — separate blast-radius boundary |
| Provisioning complexity | **Lower** — one account, one trail, one set of roles | Higher — cross-account trust, roles in both accounts |
| IAM boundary | Role in the same account as the target | Cross-account role + trust policy, which must be right |
| CloudTrail independence | Trail in the target account covers it | Trail must be configured deliberately across accounts |
| Cost | Lower | Higher — a second account's supporting resources |
| Ongoing complexity | **Lower** | Higher — two accounts to maintain and audit |
| Blast radius | Bounded by the one-role, one-ARN policy | Slightly narrower boundary, at meaningful added complexity |
| Runs the existing runbook as written | **Yes** — §2 account check matches section 1 simply | Possible, but §2's exact-match check and the trust policy both need rework |

**Recommendation: Option 1, a dedicated executor environment inside account
`527557823928`.** The target instance lives in this account, and the safety
properties the first live run needs are enforced by **policy scope and role
separation**, not by the account boundary. The mutation role is scoped to one
instance ARN and holds no other EC2 action, so a separate account would not make
that stop less capable. Option 2's extra isolation is real but is paid for with
cross-account trust — itself a mutable permission boundary — plus cost and ongoing
complexity, and none of that strengthens the property the first run actually
exercises: that one scoped principal issued exactly one dispatch and AWS-side
evidence confirms it.

A separate account would become the better answer if the requirement were *hosting
untrusted workloads*, not *proving one dispatch*.

### AWS email assessment: INFORMATIONAL — NO CURRENT SWS CONTROL IMPACT IDENTIFIED

An AWS Health notice reports that effective 2026-12-31, `ListRegions`,
`GetAccountInformation` and `GetContactInformation` move from event source
`billingconsole.amazonaws.com` to `account.amazonaws.com`.

**SWS has no control dependency on any of the three APIs or on either event
source.** Verified read-only across the repository: zero occurrences of
`ListRegions`, `GetAccountInformation`, `GetContactInformation`,
`billingconsole.amazonaws.com`, or `account.amazonaws.com` in any `.py`, `.md`,
or `.yml` file.

The reason is structural, not incidental. First-live verification filters on
`EC2_EVENT_SOURCE = "ec2.amazonaws.com"` (`reconciliation.py:75`) and requires
`event_name == "StopInstances"`. The reconciliation's entire contract is one EC2
mutation event for one target in one window; a change to which service emits
Account Management events cannot affect it. SWS also performs no Account Management
or billing API calls — the `account_id` usages elsewhere in the codebase are
inventory reads and demonstration fixtures, not credential or management paths.

**No code, test, ADR, runbook or policy change is made in response to this email,
and none is recommended.** Adding handling for events SWS never reads would be
unnecessary future-proofing, and it would touch a verified safety surface for a
non-issue. The date is recorded so that if SWS ever *does* gain an Account
Management audit requirement, this change is already known rather than a surprise.

### Provisioning contract

The single "before provisioning → before first live" checklist. **Nothing here has
been performed.** Owners are roles, not names; every item is an operator decision
or an operator-executed action.

**A. Executor**

| Item | Owner | Evidence required | Depends on | Provisioning? | Blocks first live |
| --- | --- | --- | --- | --- | --- |
| Dedicated host exists, sole purpose this run | operator | host id, region recorded in 0012 | A1–A2 | yes | **yes** |
| Instance profile carries the mutation role | operator | profile contents shown | A3, C1 | yes | **yes** |

**B. Operator access**

| Item | Owner | Evidence required | Depends on | Provisioning? | Blocks first live |
| --- | --- | --- | --- | --- | --- |
| Controlled access mechanism chosen and documented | operator | decision recorded in 0012 | A1 | decision | **yes** |
| Access cannot confer mutation capability | operator | trust policy review showing no path to the mutation role | B1, C2 | decision | **yes** |
| Approver is a named human, distinct from the operator | operator | written approval naming the instance id | G1 | no | **yes** (precondition 11) |

**C. Mutation identity**

| Item | Owner | Evidence required | Depends on | Provisioning? | Blocks first live |
| --- | --- | --- | --- | --- | --- |
| Role created with the two permitted actions only | operator | policy JSON | A2 | yes | **yes** |
| `StopInstances` resource = the one instance ARN | operator | ARN equals precondition 8's id | C1, G1 | yes | **yes** |
| `DescribeInstances` = `*`, ratified M15-E, this role only | owner (already ratified) | ADR 0009 §6 | C1 | yes | **yes** |
| No `cloudtrail:`, `iam:*`, or `sts:*` in the policy | operator | `validate_iam_policy` output | C1 | yes | **yes** |
| Role not assumable by any other principal | operator | trust-policy review | C1 | yes | **yes** (precondition 16) |

**D. Mutation IAM policy** — the artifact exists at M15-E; only re-validation and
one-ARN re-binding are outstanding (precondition 14). Owner: operator. No new
design work.

**E. Forensic identity**

| Item | Owner | Evidence required | Depends on | Provisioning? | Blocks first live |
| --- | --- | --- | --- | --- | --- |
| Separate role created, `LookupEvents` + trail-identity only | operator | policy JSON | A2 | yes | **yes** |
| No `ec2:*` in the forensic policy | operator | policy review | E1 | yes | **yes** |
| Mutating role confirmed to hold no CloudTrail read | operator | both policies side by side | C1, E1 | no | **yes** (precondition 16) |
| Reader principal recorded on retrieved evidence | operator | `CloudTrailEvidence.reader_principal_arn` set and distinct | E1 | no | **yes** |

**F. CloudTrail**

| Item | Owner | Evidence required | Depends on | Provisioning? | Blocks first live |
| --- | --- | --- | --- | --- | --- |
| Trail exists covering target account + region | operator | `DescribeTrails` returns it | account decision | yes | **yes** |
| Lookup region + trail identity fixed in Q4 in advance | operator | 0012 Decision 4 filled | F1 | no | **yes** (precondition 18) |
| Event selector includes `StopInstances` | operator | selector config | F1 | yes | **yes** |

**G. Target**

| Item | Owner | Evidence required | Depends on | Provisioning? | Blocks first live |
| --- | --- | --- | --- | --- | --- |
| Sacrificial instance created and disposable | operator | instance id, tags | A1 | yes | **yes** |
| All 18 preconditions recorded pass with evidence | operator | runbook §1 table | G1, C2, E2, F2 | no | **yes** |

**H. Verification**

| Item | Owner | Evidence required | Depends on | Provisioning? | Blocks first live |
| --- | --- | --- | --- | --- | --- |
| `sts:GetCallerIdentity` matches expected role ARN | operator | §2 record | A2, C1 | no | **yes** |
| Witness arms before dispatch | SWS (exists) | preflight finding clear | G1 | no | **yes** (precondition 13) |
| Effective retries read as one attempt at dispatch | SWS (exists) | `effective_retries` output | G1 | no | **yes** (precondition 15) |
| Reconciliation returns `CONFIRMED` | SWS (exists) | `require_reconciliation` passes | E4, F1 | no | **yes** |
| Live mutation separately authorized in writing | operator | 0012 authorization box | all above | no | **yes** |

Every item blocks. That is the honest state: the chain has no short path, and the
first live stop is gated on all of it.

### Boundary of this subsection

Q1 remains **OPEN WITH A DEFINED TARGET ARCHITECTURE AND A DEFINED IDENTITY
CHAIN.** The architecture is now specified well enough to provision in one
coherent phase, and nothing has been provisioned. Q2, Q3, Q4 and Q5 remain
**OPEN**; this subsection designs what Q2 and Q3 will require but resolves neither.
No AWS resource was created, modified or deleted. Operator authorization is not
granted and the live gate remains closed.

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
Checkpoint:            5e4215f (pushed to origin/main)
Environment record:    POPULATED — 4 hard blockers, all decisions OPEN
Implementation:        FROZEN
Q1 target architecture: A — dedicated AWS execution environment (NOT provisioned)
Live mutation:        NOT AUTHORIZED
AWS mutations:        NONE across the whole investigation. Read-only calls only:
                      sts get-caller-identity (twice), cloudtrail describe-trails
```

### Blockers

Four things must exist before this record can be completed. All are AWS or
infrastructure mutations and none is authorized here. Q1 is no longer simply
"undecided" — it now has a recommended target architecture, but that architecture
does not exist, so the blocker is unchanged in substance.

1. **No CloudTrail trail exists** (Q4). Without one there is no AWS-side evidence,
   and Policy A would mature an empty result into a false "no dispatch" finding.
   A trail covering the target account and region is a precondition for the entire
   M15-G verification architecture.
2. **The only reachable identity is account root** (`arn:aws:iam::527557823928:root`).
   It cannot be scoped to one instance ARN, so it can never be the mutation
   principal (Q2), and using it as the forensic reader would collapse the
   independence Q3 requires. A dedicated scoped mutation role and a separate
   read-only forensic role are both needed.
3. **No executor environment exists** (Q1). Four candidates were investigated and
   none is usable today; the recommended target architecture (A, a dedicated AWS
   execution environment) has not been provisioned. Until an executor exists there
   is also no target instance, account, or region, and Q2 and Q3 both depend on it.
4. **No mutation identity or forensic reader exists** (Q2, Q3). These are separate
   from blocker 2 and separate from each other: a scoped mutation role *and* a
   distinct read-only forensic role are both required, and neither can be created
   before an executor exists to attach them to.

**Next action:** approve or reject target architecture **A**, then decide **same
account versus a separate executor account**, then the attachment mechanism. That
is the smallest operator choice that unblocks the chain; everything downstream
follows from it. No runbook step may be attempted until Q1 through Q5 are
resolved and the live mutation is separately authorized.