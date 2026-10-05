# Reuse decisions — SWS vs SMS

The existing **Semantic Memory Steward (SMS)** repository
(`https://github.com/Synthesis-works/Semantic-Memory-Steward`) is a frozen,
read-only architecture reference. SWS selects only **documented,
domain-agnostic patterns** from it. It is not a copy or a mass rename.

For every component reused from SMS, this document records:

1. What was reused
2. Why it is domain-agnostic
3. What was changed for SWS
4. What was intentionally rewritten instead of copied

## Reused patterns

| Component | What was reused | Why domain-agnostic | Changed for SWS |
|---|---|---|---|
| `trace.py` | Ordered, append-only execution-event sink; truthfulness contract (no fabricated events); `to_dicts`/`from_dicts` serialization | Recording "what actually happened" is not specific to any domain | Event vocabulary rewritten: SMS stage names (DISCOVERY/AI_ANALYSIS/POLICY/HUMAN_APPROVAL) replaced with `TraceEventType` (inventory_query, action_execution, verification, audit, ...) |
| `authorization.py` | Deterministic, rule-based safety gate returning AUTHORIZED / PENDING_APPROVAL / BLOCKED; never LLM-decided; mode-aware | A deterministic boundary between an LLM and side effects is generic | All rules rewritten for SWS's AWS action vocabulary, resource types, and SAFE/REVIEW/AUTONOMOUS modes |
| `interfaces.py` | Protocol-based decoupling convention | Protocols decoupling orchestration from implementations are generic | All interfaces written fresh for the AWS workspace domain |
| `memory.py` | Fail-fast config validation (partially configured subsystem raises) | Failing early on bad configuration is generic | New SWS config schema; bounds reference canonical limits in `constants.py` |
| `relationships.py` | Deterministic evidence takes precedence over semantic inference | The precedence rule is generic | Fresh implementation for AWS `ResourceRecord` relationships |
| test conventions | Hermetic tests; autouse environment guard; injected fakes; invariant-pinning tests | Credential-free, deterministic testing is generic | SWS-specific tests and fixtures written fresh |
| `.github/workflows/ci.yml` | Hermetic pytest on `main` + PRs, Python 3.12, editable `.[dev]` install | Standard packaging/CI practice | Fresh workflow for this repository |
| `pyproject.toml` | src-layout packaging, dev extra, editing-based tooling | Standard practice | Minimal dependency set (only `pydantic` runtime + `pytest` dev); `boto3` stays an optional extra (`aws`) used only by the real client factory (`aws.py`), and the MCP SDK extra is implemented and used by the M4 server (`mcp` extra) |

## Intentionally rewritten (not copied)

- **Action vocabulary** — SWS uses `LEAVE / FLAG_FOR_REVIEW / REQUEST_APPROVAL / STOP_RESOURCE` for AWS resources. SMS's file-lifecycle states (KEEP / ARCHIVE / TRASH / QUARANTINE) are deliberately banned; `constants.SMS_DEPRECATED_ACTIONS` guards this in tests.
- **Domain models** — all `models.py` records (ResourceRecord, PolicyDecision, AuthorizationRequest, CostEstimate, ...) are new.
- **Policy engine** — SMS's retention heuristics are not portable. SWS
  implements the `PolicyEngine` protocol fresh in `policy.py` as the
  deterministic `WorkspacePolicyEngine` (rules `missing_owner_tag` /
  `owner_unverifiable`, M2C-D); no LLM ever decides.
- **Semantic layer, embeddings, provider adapters** — SMS-specific and excluded.
- **Web simulator / dashboard** — SWS does not adopt SMS's Streamlit
  dashboard. Its M5 layer is a minimal vanilla HTML/JS page that talks to the
  real MCP server (see "MCP server (M4)" and "Web simulator (M5)" above).

## Intentionally excluded from SWS

- SMS's file-lifecycle policy states and policy engine
- Streamlit dashboard and UI state
- Local filesystem inventory and document models
- Document-content ingestion and Comprehend processing
- S3 Vectors / embedding architecture and provider fallbacks
- Legacy CLI entry points and root-level debug scripts
- SMS dependencies not needed now (pypdf, openpyxl, streamlit, pandas, altair)

> Note: `strands-agents` is no longer on this exclusion list. It is an
> **optional** dependency of the M3-A explanation layer (see "Optional layers"
> below), not a runtime requirement of SWS.

## Optional layers

- **Explanation (M3-A)** — Strands (`strands-agents`) backs the optional
  natural-language explanation layer behind the `ExplanationProvider`
  protocol. It is an **optional** dependency installed via the `explanation`
  extra; `NullExplanationProvider` remains the default, so SWS functions
  fully with no LLM. `bedrock_explanation.py` never imports `strands` at
  module import time (`build_strands_agent` imports it lazily), and the
  injected-agent boundary keeps the hermetic suite free of AWS/Bedrock calls.
  Explanations are `ClaimKind.INTERPRETED` only and never influence policy,
  authorization, or risk decisions. Strands is the agent layer by design;
  later agent capabilities (MCP/AgentCore) build on it rather than replacing
  it with a direct boto3 caller.

- **MCP server (M4)** — `sws_agent.mcp.server` exposes SWS through a real MCP
  server on the **Streamable HTTP** transport (MCP SDK v2, optional `mcp`
  extra + `uvicorn`; FastMCP and MCP v1 are deliberately not used). The MCP
  layer is **transport/exposure only**: every tool is a thin adapter over the
  deterministic SWS core (inventory, relationships, policy, cost, approval),
  and the core is the sole source of truth. Tools are stateless and carry
  structured snapshot data with the client; distrusted input fails
  deterministically via `ToolError`. Explanations keep flowing only from
  `ExplanationProvider` (see above) and can never re-derive a decision.
No action-execution tool is exposed; the approval ticket lifecycle is the
   extent of state mutation and never touches AWS. There is no authentication
   yet — that boundary is documented for the AgentCore Harness (which consumes
   this server via its remote MCP integration) to solve at deployment.

- **Web simulator (M5)** — `sws_agent.simulator` is a local, browser-based
  demo **over the real M4 MCP server**: the starlette web app drives the
  actual Streamable HTTP endpoint via the official MCP SDK client, and the
  server's `SwsBackend` is the deterministic `DemoBackend` (synthetic
  inventory feeding the real policy/relationships/approval core). The demo
  deliberately does NOT reuse SMS's Streamlit dashboard. It is a minimal
  vanilla HTML/JS single page with a deterministic rule-based router and a
  real per-session tool trace. All replies are derived from real MCP results
  (failures surface as explicit errors, never fabricated success); the
  dataset, cost figures, and explanations are clearly labeled synthetic; no
  AWS access or action execution is possible; `decide_ticket` only updates an
  in-memory store. Optional `simulator` extra (`starlette`, `mcp`, `uvicorn`);
  no listeners on import (startup via `python -m sws_agent.simulator`); the
  MCP-bound validation lives in `tests/test_simulator_mcp.py`.

- **Real AWS client factory (M6)** — `sws_agent.aws` is the adapter between
  boto3 and the already-existing collector client protocols, closing the only
  production gap reported by the M6 investigation. It exposes exactly the six
  collector methods (`AwsMultiClient`) behind a lazy `AwsClientFactory` that
  consumes `AWSConnectionConfig` and the `AWS_API_RETRY_ATTEMPTS` /
  `AWS_API_TIMEOUT_SECONDS` limits. boto3/botocore are imported lazily only at
  tool execution time (optional `aws` extra); import and default startup stay
  credential-free, clients are injected through the existing `SwsBackend`
  seam, and no MCP/policy/collector behavior changed. Real mode is opt-in via
  `SWS_AWS_REGION` / `SWS_AWS_PROFILE` when launching the M4 server
  (`python -m sws_agent.mcp.server`).

- **Action planning workflow (M7)** — `sws_agent.workflow.ActionPlanner`
  makes the pre-existing deterministic authorization gate reachable end to
  end: it maps a candidate `(resource, action)` through `ActionAuthorizer`
  and, whenever the gate requires human approval, creates a PENDING ticket
  in the approval store. It is bounded strictly pre-execution — it plans,
  it never executes (no `ActionExecutor` exists; `ActionPlan.executed` is
  always `False`) and makes no AWS calls. The execution mode is an operator
  setting (`SWS_EXECUTION_MODE`, validated through `SWSRuntimeConfig`)
  fixed at backend construction — never a per-request client input — so
  callers cannot widen autonomy. The MCP surface gains one additive tool
  (`request_approval`) and `get_cost_estimates` now honestly reports
  `truncated`/`failures` from the same trace semantics as workspace audit.
  Existing tool contracts are unchanged except additive keys.

- **Durable audit ledger (M8)** — `sws_agent.audit` is a fresh, stdlib-only
  append-only JSONL ledger (no SQLite, no rotation, no deletion) stamped at
  write time with `record_id`/`created_at` and sanitized payloads (no
  `ResourceRecord.raw`, explanation prose, credentials, wire payloads, or raw
  Cost Explorer pages). Lineage ids (`run_id`, `snapshot_id`, deterministic
  `decision_id`, `action_plan_id`, ticket `plan_id`) are additive to the
  existing domain models, so every existing construction remains valid. The
  MCP backend writes through to the ledger as a side effect after each
  successful deterministic result — request/response shapes are unchanged —
  and persistence failures surface as `ToolError` (fail-loud, never a silent
  drop; a failed tool call itself writes nothing). Persistence is opt-in
  (`audit_store` injection or `SWS_AUDIT_DIR`); the hermetic no-store default
  is preserved. M8 is read-only persistence: no `get_history`, no execution
  records (the envelope's `execution` stanza is reserved for M9), no new
  runtime dependency.

## SMS-specific functionality NOT copied

SMS itself (its governance product, pipeline, agent, evaluation harness, demo
corpus) is not part of SWS in any form.

## Experiments lesson applied day one

Throwaway experiments live in `scripts/experiments/` (gitignored, with a
`.gitkeep`), never in the repository root.

## Canonical definitions lesson applied day one

Every important state, label, action, and limit is defined once in
`src/sws_agent/constants.py` and imported everywhere. Tests assert the action
vocabulary never drifts into SMS's file-lifecycle states.

## Execution boundary (M9)

- **New vocabulary, new boundary.** `ExecutionStage`, `ExecutionOutcome`,
  `VerificationStatus`, and `RefusalReason` are fresh SWS definitions (with
  `SWS_SUPPORTED_*` frozensets guarded by tests, matching the canonical-label
  rule). Nothing is inherited from a file-lifecycle stage model.
- **The sketch `interfaces.ActionExecutor` was left untouched but is
  superseded.** It is still the minimal M4-era sketch; the real boundary is
  `sws_agent.execution.MutationHandler` plus `ExecutionCoordinator`. It is not
  wired anywhere and carries no behavior, so changing it would only churn
  unrelated history.
- **Lineage was reconciled, not duplicated.** M9 reuses the M8 identifiers
  (`decision_id`, `snapshot_id`, `run_id`, `action_plan_id`, `plan_id`,
  `ticket_id`). The only genuinely missing linkages were added as optional
  fields: `ActionPlan.decision_id/snapshot_id/run_id` (stamped by the planner
  from the supplied `PolicyDecision`) and `ApprovalTicket.consumed` (one-way
  exactly-once redemption). No parallel id scheme was introduced.
- **No real AWS mutation in M9 (deliberate).** The read-only investigation
  concluded no first action is safe to enable: `ec2:StopInstances` is the
  natural future mapping but SWS has no EC2 surface, and adding one purely to
  have something to verify would be unsafe. M9 therefore registers no
  `MutationHandler`, every `ActionSpec.implemented` is `False`, and the
  registry's `mutation` string is documentation only. The ledger contract
  (PRE/ATTEMPT/RESULT/POST) is validated with injected fakes, and the A5
  freshness claim is exercised per-test with a fake observation harness.
- **No schema change.** `AuditEnvelope` and `AUDIT_SCHEMA_VERSION` are
  untouched: the reserved `execution` stanza stays `None` and M9 uses the
  existing kind/payload structure with a new `AuditRecordKind.EXECUTION`.

## Re-execution semantics (M15-B)

- **New vocabulary, orthogonal to the existing one.** `ReexecutionClass`
  (`execution_ledger.py`) is a fresh SWS definition and is the second member of
  that canonical-label pair: `ExecutionOutcome` records *what was observed*,
  `ReexecutionClass` records *whether repeating the same intent is safe*.
  Nothing is inherited from SMS — a retention model has no external-effect
  boundary to re-execute across, so the concept does not exist there.
- **The distinction was not reused from SMS's idempotence or retry handling.**
  SMS operates on files, where re-running a failed step is idempotent by
  construction and `UNKNOWN` is recoverable by re-reading the filesystem.
  SWS crosses an irreversible external boundary, so idempotence is a property to
  be *established per action* rather than assumed. That is why the basis is a
  recorded fact with a closed vocabulary instead of a retry flag.
- **The migration reconstructs, it does not re-decide.** The v2→v3 backfill
  derives each row's basis from the retryability version 2 actually applied. A
  `failed` row becomes `TRANSIENT_REJECTION`, not the `TARGET_INVALID` the new
  rules would assign to a fresh attempt, because re-deciding old rows would
  rewrite a file's history in a direction nothing in it supports.
- **No parallel state store.** The basis is a column on the existing
  `execution_reservation` table and is written by the same statement as the
  outcome, so there is no second source of truth about whether a row permits a
  retry. See ADR 0007.

## Dispatch evidence and classification (M15-C)

- **New vocabulary, orthogonal to the existing one.** `DispatchDisposition`
  (`constants.py`) and `DispatchEvidence` (`models.py`) are fresh SWS
  definitions. `DispatchDisposition` records *what the boundary learned about the
  crossing*; `ReexecutionClass` records *whether repeating the intent is safe*;
  `ExecutionOutcome` records *what was observed*. Three orthogonal questions, none
  of which can be derived from another.
- **The disposition table is not reused from SMS.** SMS has no external-effect
  boundary to characterise: its steps act on files, where "was this dispatched"
  has no ambiguous middle, because a partially written file is re-read rather than
  re-dispatched. SWS crosses an irreversible HTTP boundary where the request may
  have been applied and the response lost, so the disposition must say *which*
  of those happened rather than defaulting to success.
- **`MutationAttempt` was extended, not wrapped.** Replacing its booleans in
  place, rather than adding a parallel evidence object beside them, avoids two
  ways to describe one crossing and makes the fail-closed direction structural:
  there is no longer an object with no disposition, so there is nothing for the
  coordinator to interpret as a claim of success.
- **Retry policy is not reused from botocore's retry policy.** botocore retries
  `RequestTimeout`, 5xx, and throttling codes — a read-side policy applied to a
  mutation, where each retry is another dispatch. `classify_dispatch` has no
  retry loop and no attempt counter; repeatability is a *recorded basis* produced
  once from evidence, so "how many times did we dispatch" stays a fact about the
  call rather than a property of an SDK default.
- **The per-action tables are deliberately empty.** `DispatchContract` is a
  carrier for an allowlist, not a default policy, and no action populates it.
  Reusing botocore's modelled-error list as the rejection table was rejected: that
  list describes what the API *can* return, not what each code means for re-running
  *this* intent against *this* target.
- **No parallel state store.** The classified basis is passed straight to
  `record_outcome` as its required argument. `classify_dispatch` is the only
  function in `src/` that produces a `ReexecutionClass`, so there is no second
  derivation that could disagree with the ledger. See ADR 0007's amendment.