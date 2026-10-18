# ADR 0015: Public demo conversational LLM (Strands + Amazon Bedrock, synthetic workspace)

- Status: **accepted.** Configuration is committed (`deploy/web/Dockerfile`,
  `render.yaml`, `sws_agent.simulator`). The Render service, its secret
  credentials, and the DNS record remain manual operator steps. **No AWS
  infrastructure is created or modified by this ADR**, and the M17
  MCP/AgentCore deployment is untouched.
- Milestone: public demo website (post-M17 deployment task), follow-up to ADR 0014
- Date: 2026-10-17
- Depends on: M5 (`sws_agent.simulator`), the conversational seam added in
  commit `0996598` and hardened in `77823cc`, ADR 0014 (Render hosting).

## Context

ADR 0014 hosted the M5 simulator on Render **synthetic-only and
deterministically** (no LLM). The public site therefore answered with the
rule-based router (`sws_agent.simulator.router`), and the UI said so
("rule-based demo router with no autonomous LLM"). The conversational
architecture (a bounded, validated model loop over the read-only MCP tool
catalog) already existed in the repo but was unreachable from the public site:
`SWS_AGENT_MODEL` was unset and the image installed only the `simulator` extra,
so the Strands provider was not even present.

The operator explicitly authorized a **real, low-cost conversational LLM** on
the public demo, while keeping the SWS workspace synthetic and the LLM
incapable of any AWS resource access or mutation.

## Decision

Enable the **existing, real Strands + Amazon Bedrock conversational provider**
on the public Render demo, with the smallest possible blast radius.

- **Real provider, not a mock.** No deterministic "conversational" stand-in is
  built. `agent_from_env()` selects the existing `StrandsConversationAgent`
  when `SWS_AGENT_MODEL` is set (and `SWS_AGENT_PROVIDER` is `strands`/unset).
  Unset → the simulator stays deterministically rule-based.
- **Model: `amazon.nova-lite-v1:0`, region `us-east-1`.** Nova Lite is AWS's
  low-cost model; a bounded one-shot JSON action/answer keeps per-message cost
  negligible. The region is read from `AWS_REGION` →
  `AWS_DEFAULT_REGION` → `us-east-1`.
- **Cost ceiling in code.** `AGENT_MAX_TOKENS = 768`, the loop stays bounded at
  `AGENT_MAX_STEPS = 6`, history is capped at `AGENT_HISTORY_LIMIT = 16`, no
  retries (`retry_strategy=None`), no streaming, no background/scheduled
  inference, no RAG/embeddings/memory.
- **Synthetic workspace preserved.** The demo still runs `DemoBackend` behind an
  **in-process** MCP server. `SWS_MCP_URL` is never pointed at
  `mcp.sjsujalgupta.xyz`; the live MCP deployment is not used.
- **MCP boundary unchanged.** The model sees a hard-coded catalog of 8 read-only
  tools. `decide_ticket`, execution, and mutation tools are absent. The existing
  validation (`validate_agent_tool_call`, snapshot injection, forbidden-key
  stripping) stays authoritative.
- **Explicit approve/deny stays deterministic.** The UI's explicit
  approve/deny demo commands (`Approve this ticket …` / `Deny …`) continue to
  run through the deterministic `_run_decide` path; the LLM is never allowed to
  decide a ticket. An LLM-proposed `decide_ticket` is refused.
- **Separate Bedrock-only identity.** Render receives a dedicated IAM identity
  whose only permission is `bedrock:InvokeModel`/`Converse` on the chosen
  model. It is **not** the SWS execution role. Credentials arrive only as
  Render **secret** environment variables; nothing is baked into the image or
  committed.
- **Honest UI/state.** The demo badge/footer reflect the active mode and the
  real provider ("conversational · Amazon Bedrock"); `/api/meta` reports
  `mode`, `provider`, `provider_label`, and `model`. A failure degrades to an
  explicit "conversational model is unavailable" response rather than pretending
  a model answered.

## Supersedes

This ADR supersedes the ADR 0014 statements that the public image contains **no
boto3** and **no AWS credentials**. With the conversational provider enabled,
the image includes `strands-agents` (which pulls `boto3` transitively for
Bedrock inference), and the Render service holds a **Bedrock-only** credential.
Everything else in ADR 0014 (free external host, separation from M17, the
CNAME-only DNS change, no health endpoint, no auth) still holds. The SWS
workspace stays synthetic and the demo still reaches **no AWS resource API**.

## What this ADR does **not** do

- No AWS resource (IAM/ECS/ECR/ALB/Route 53) is created or modified here; the
  Bedrock-only identity is documented in the runbook as a manual operator step.
- No change to the nine-tool MCP boundary, `decide_ticket` exclusion, the SWS
  execution semantics, or any frozen v1 invariant.
- No change to M17 MCP/AgentCore or `mcp.sjsujalgupta.xyz`.
- No AgentCore, no EC2/S3/Lambda/Cost Explorer/CloudTrail access.

## Consequences

- **AWS credits are now used**, but only for Bedrock inference with Nova Lite.
  The public demo generates one bounded inference workflow per user message.
- **Budget exposure is bounded by code** (token/step/history ceilings, no
  retries) and by IAM (Bedrock-only, single model), not by hope. The free
  Render plan adds no cost.
- **Cold starts** (~1 min after idle) still apply; the first message after a
  cold start also pays the model's first-call latency.

## Verification

- Hermetic tests cover provider selection, the Nova Lite defaults, the token and
  iteration ceilings, `decide_ticket` exclusion, deterministic approve/deny
  routing, honest degradation, and that the simulator imports no AWS resource
  SDK. See `tests/test_simulator_agent.py`, `tests/test_simulator_service.py`,
  and `tests/test_simulator_main.py`.
- A separate, manual **one-shot** real Bedrock rehearsal (Nova Lite) exercises
  the live model with mocks otherwise disabled; the full suite never depends on
  a paid call.
- `https://sws.sjsujalgupta.xyz` is reported live only after the manual Render +
  DNS + credential steps succeed.
