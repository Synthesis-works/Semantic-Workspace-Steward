# AWS services in SWS — what is real, what is synthetic, what has executed

This document separates three things that are easy to conflate, because the SWS
project keeps them deliberately apart:

- **A. AWS-backed capabilities that exist in the code** — what the repository can
  call, and under which least-privilege scope.
- **B. The public demo** — what `https://sws.sjsujalgupta.xyz` actually does
  (synthetic workspace, one Bedrock inference call, no AWS resource access).
- **C. Real execution evidence** — the only things that have ever touched a real
  AWS account, each recorded.

Every statement below is traceable to a file, ADR, decision record, or commit in
this repository. Where a capability exists in code, "exists" means the code path
is present and tested; it does not mean a live resource exists.

Sources: `README.md`, `docs/adr/0011-cloudtrail-independent-verification.md`,
`docs/adr/0014-public-demo-website-hosting.md`,
`docs/adr/0015-public-demo-conversational-llm.md`,
`docs/decisions/0012-first-live-stop-environment-decision-record.md`,
`render.yaml`, `deploy/web/Dockerfile`, `src/sws_agent/aws.py`,
`src/sws_agent/inventory.py`, `src/sws_agent/cost_explorer.py`.

---

## A. AWS-backed capabilities that exist in the code

These are implemented collector/seam paths in the repository. They are the
product's read-only AWS surface. Whether any given one has been run against a live
account is section C, not this section.

| Capability | Where | AWS API calls | Notes |
| --- | --- | --- | --- |
| S3 inventory | `src/sws_agent/inventory.py`, client seam `src/sws_agent/aws.py` | `ListBuckets`, `GetBucketLocation`, `GetBucketTagging` | Read-only. No `GetObject` — SWS never reads inside a bucket. |
| Lambda inventory | `src/sws_agent/inventory.py`, seam `src/sws_agent/aws.py` | `ListFunctions`, `ListTags` | Read-only. |
| Account-level cost | `src/sws_agent/cost_explorer.py` | `GetCostAndUsage` | Read-only, may paginate via `NextPageToken`. |
| EC2 instance observation | `src/sws_agent/ec2_observation.py` | `DescribeInstances` (M11 read seam) | A single-target read seam, not a general account-wide EC2 inventory collector (that collector is deliberately deferred; see `docs/reuse-decisions.md`). |
| Conversational model call | `src/sws_agent/simulator/agent.py` (`_model_invoker`) | Bedrock `InvokeModel` / `Converse` on `amazon.nova-lite-v1:0` (us-east-1) | Non-streaming (`streaming=False`, commit `31d4223`); requires only `bedrock:InvokeModel`. |

Notable honesty points about this surface:

- The read-only allowlist above contains **no** data-plane read (no `GetObject`,
  no function invocation) and **no** mutating action. SWS inspects resource
  *metadata*, not resource *contents*.
- The public conversational LLM cannot touch any of these. It is given a
  hard-coded read-only tool catalog and may only produce prose; the deterministic
  rules decide, the model rephrases.

---

## B. The public demo — synthetic by construction

`https://sws.sjsujalgupta.xyz` runs the existing simulator image on Render
(`render.yaml`, `deploy/web/Dockerfile`), per ADR 0014 and ADR 0015.

- **Synthetic workspace.** The demo drives a real MCP tool boundary over an
  **in-process** synthetic backend (`DemoBackend`) with a fixed, seeded workspace.
  The SWS collectors in section A are never used by the demo because the backend
  is synthetic.
- **No AWS resource access.** The demo cannot read, write, or mutate any AWS
  resource. It does not call `mcp.sjsujalgupta.xyz` or any AgentCore endpoint.
- **One AWS call only: Bedrock inference.** The conversational layer makes a real
  Amazon Bedrock model call (`bedrock:InvokeModel` on `amazon.nova-lite-v1:0`,
  region `us-east-1`) under a separate, **Bedrock-only** IAM identity. This is a
  model call, not a workspace read or write. Credentials arrive only as Render
  **secret** environment variables; nothing is baked into the image or committed.
- **No mutation path.** `decide_ticket` is excluded from the agent tool catalog,
  and the demo's explicit approve/deny stays deterministic. The model is never
  allowed to decide a ticket; an LLM-proposed `decide_ticket` is refused.
- **Honest mode reporting.** The UI badge/footer and `/api/meta` report the
  actually-active mode and provider ("conversational · Amazon Bedrock"). If the
  model is unavailable, the demo says so explicitly rather than pretending a model
  answered.

In short: on the public demo, the AWS-adjacent thing that is *real* is the Bedrock
inference call. Everything about the inspected "cloud workspace" is simulated.

---

## C. Real execution evidence — the only things that have touched a real AWS account

Recorded in `README.md` ("What has actually run in a real AWS account") and, in
full, in `docs/decisions/0012-first-live-stop-environment-decision-record.md`.
This section is intentionally narrow.

1. **Read-only collectors via guarded probes.** The client factory
   (`src/sws_agent/aws.py`) can run the S3/Lambda inventory and Cost Explorer
   collectors against a live account, exercised through gitignored, read-only
   probes in `scripts/experiments/` (`ListBuckets`, `GetBucketLocation`,
   `GetBucketTagging`, `ListFunctions`, `ListTags`, `GetCostAndUsage`, and optional
   `sts:GetCallerIdentity`). No writes.

2. **Exactly one live mutation, run once.** A single EC2 `StopInstances` through
   the fixed live entry point (`src/sws_live/`, console script `sws-live-stop`),
   under the signed-approval gate, on **2026-10-11**. Per decision record 0012
   §12:
   - The record is the environment provisioned 2026-10-10 (§11) and the first live
     `StopInstances` executed and independently verified 2026-10-11 (§12).
   - The mutation executed as the dedicated, single-target mutation role
     (`sws-live-stop-mutation-role`); the operator dispatched only via SSM; the
     forensic read used a separate, non-mutating identity.
   - AWS-side verification (CloudTrail) confirmed **exactly one** `StopInstances`
     event for the intended principal, at the pinned target, within the declared
     window, corroborated by the target's own state transition
     (`stopped`, `StateTransitionReason = "User initiated (2026-10-11 04:54:01 GMT)"`).
   - `STOP_RESOURCE.implemented` remains `False`. That path is not MCP-reachable
     and is not part of the demo.
   - Account `527557823928`, region `us-east-1`.

3. **Amazon Bedrock for the conversational layer.** The public demo calls
   `bedrock:InvokeModel` on `amazon.nova-lite-v1:0` (us-east-1) under a Bedrock-only
   IAM identity, as described in section B. A model call, not a workspace
   read/write.

No other AWS call has been made by this project, and no fabricated live evidence
appears anywhere. This is the same honesty boundary the repository states in its
README.

---

## How this document relates to the code claims

- The repository defines **no infrastructure-as-code** for AWS (no
  Terraform/CDK/SAM/Pulumi/Ansible). Decision record 0012 §"Evidence sources used"
  records this as a verified negative fact. So nothing in this document implies
  that a resource exists merely because a config or code path references it.
- Where a capability appears in code (section A) and has *not* been run live, that
  is stated; where something has run live (section C), the evidence is recorded in
  decision record 0012.

## What this document does **not** claim

- It does not claim the public demo can mutate AWS (it cannot; section B).
- It does not claim the LLM can call the AWS SDK/collectors directly, or bypass the
  approval boundary (it cannot; it may only produce prose over a synthetic
  read-only catalog).
- It does not claim Alexa+ is integrated. The hackathon voice mode is an
  Alexa+-inspired **browser** speech simulation; see `README.md` ("Hackathon:
  Alexa+ track").
- It does not claim any capability is production-ready beyond what the recorded
  evidence supports.
