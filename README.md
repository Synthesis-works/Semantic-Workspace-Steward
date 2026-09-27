# Semantic Workspace Steward (SWS)

Semantic governance and workspace intelligence for an **individual AWS account**.

SWS answers questions like:

- What AWS resources are connected to my project?
- Which resources appear unused or redundant?
- What might be costing me money?
- Why is this resource important?
- What can I safely clean up?
- What would happen if I stopped or changed this resource?

The product combines AWS resource inventory, deterministic metadata and
metric collection, resource relationship analysis, semantic reasoning where
it provides genuine value, cost and risk interpretation, deterministic policy
evaluation, human approval, narrowly scoped actions, verification, and audit
with persistent workspace memory.

## Core philosophy

> **AI understands. Deterministic policy decides.**

The LLM may explain and interpret information, but it must never authorize
risky AWS operations. Every side-effecting action passes through a
deterministic, independently testable policy and authorization boundary.

## Current scope (this repository)

SWS is built up in feature milestones. What is implemented so far:

- **M1/M2** — domain foundation: canonical labels, pydantic models, trace
  recording, protocol interfaces, deterministic authorization, relationship
  derivation, workspace collection, and the deterministic policy engine.
- **M2C/M3/M4** — a real MCP server over Streamable HTTP
  (`src/sws_agent/mcp/server.py`) exposing **nine tools**
  (`audit_workspace`, `collect_workspace`, `get_relationships`,
  `evaluate_workspace`, `get_cost_estimates`, `explain_resource`,
  `list_approvals`, `decide_ticket`, `request_approval`) over a thin
  `SwsBackend` seam. It is wired, invoked, and tested against the official
  MCP SDK v2 client. AWS collectors are still injected via a backend, never
  hard-wired.
- **M5** — a local, demo-only **web simulator** (below) that talks to the
  real M4 MCP server through the official MCP client.
- **M6** — a **real AWS client factory** (`src/sws_agent/aws.py`) that adapts
  boto3 clients (lazily imported, optional `aws` extra) to the existing S3 +
  Lambda + Cost Explorer collectors through the existing `SwsBackend`/MCP
  injection seam. No collector, policy, or MCP contract changed. Real-AWS mode
  is opt-in via `SWS_AWS_REGION` / `SWS_AWS_PROFILE` (see below); the hermetic
  default and the deterministic no-factory `ToolError` are preserved.
- **M7** — a **safe action-planning & authorization workflow (pre-execution)**:
  the new `request_approval` tool plans one candidate action through the
  deterministic `ActionAuthorizer` gate, returns the decision, and opens a
  `PENDING` approval ticket whenever the gate requires human approval. The
  operator-level `SWS_EXECUTION_MODE` env var (safe/review/autonomous) fixes
  autonomy at server construction — never per request — so a caller can't
  widen it. Nothing executes: `ActionPlan.executed` is always `False`, no
  `ActionExecutor` exists, and `get_cost_estimates` now honestly surfaces
  truncation and per-collector failures (matching the workspace audit
  contract) instead of silently dropping them.
- **M8** — a **durable audit ledger + lineage**: every tool now carries
  stable identifiers (`run_id`, `snapshot_id`, deterministic `decision_id`,
  `action_plan_id`, ticket `plan_id`) and, when an audit store is configured
  (backend injection or `SWS_AUDIT_DIR`), write-through persists each
  successful result to an append-only UTF-8 JSONL ledger
  (`src/sws_agent/audit.py`): runs, snapshots, decisions, plans, tickets,
  explanations, and cost collection. Persistence is a side effect —
  tool request/response shapes are unchanged — and a persistence failure
  surfaces as a `ToolError` (never a silent drop). No ledger rotation or
  deletion exists in M8: audit history never silently disappears.

It does **not** yet:

- deploy or create AWS infrastructure
- run AWS collectors against a live account by default: real mode is opt-in
  (`SWS_AWS_REGION` / `SWS_AWS_PROFILE`) and has only ever been verified
  through guarded, read-only gitignored probes in `scripts/experiments/`
- implement destructive actions or the full action engine
- expose the simulator or MCP server on anything but local loopback

Deliberately deferred (documented in `docs/reuse-decisions.md`): EC2/EBS
inventory collectors, AWS authentication/deployment, per-resource cost
attribution, and non-loopback deployment. S3 and Lambda inventory and
account-level Cost Explorer collection are implemented (M2C); the M6 real AWS
client factory is the production link that runs them against a live account.
Nothing in this repository ever fabricates AWS usage, savings, or history.

## Planned MVP AWS scope

1. Amazon S3
2. EC2/EBS **or** Lambda (second resource type)
3. AWS Cost Explorer

## Interfaces

SWS is built around an MCP server as a primary interface:

```
Web simulator/client
        |
        v
MCP server (Streamable HTTP)   <- thin adapter, real and tested
        |
        v
SWS orchestration layer
        |
        v
AWS inventory and analysis
        |
        v
Deterministic policy engine
        |
        v
Authorization and human approval
        |
        v
Action engine -> AWS APIs -> Verification and audit
```

The MCP layer (see `src/sws_agent/mcp/`) remains separate from business logic.
The real Streamable HTTP server is wired (M4) and validated over the official
MCP SDK client, so the server-side transport claim is genuine and tested.

## M5: interactive demo simulator

A local, browser-based demo on top of the **real** M4 MCP server.

```
Browser (vanilla HTML/JS)
        |
        v
SWS web simulator (starlette, src/sws_agent/simulator/)   --POST /api/chat-->
        |   deterministic rule-based router (no LLM)
        v   official MCP Streamable HTTP client
M4 MCP server (sws_agent.mcp.server.SwsMcpServer)  /mcp
        |
        v
DemoBackend (synthetic inventory) -> REAL policy/relationships/approval core
```

The simulator never reimplements SWS: it drives the actual M4 tool boundary.
Its backend is a **synthetic, deterministic demo dataset** of 20 resources
(12 S3 buckets + 8 Lambda functions) seeded through the real policy engine so
that exactly two unattributed resources are flagged for review. Every reply,
block, and trace entry is derived from the real MCP tool result returned over
the wire; malformed or failed tool results are surfaced as explicit errors,
never as fabricated success. No AWS access, no real costs, no actions execute.

### Run

```bash
pip install -e ".[dev,mcp,simulator]"
python -m sws_agent.simulator
```

This starts two local-only processes:

| process | address | purpose |
| --- | --- | --- |
| M4 MCP server | `http://127.0.0.1:8765/mcp` | real SwsMcpServer over DemoBackend |
| Web simulator | `http://127.0.0.1:8080` | the demo UI |

Environment overrides: `SWS_MCP_HOST`/`SWS_MCP_PORT`, `SWS_SIM_HOST`/
`SWS_SIM_PORT`, and `SWS_MCP_URL` (set it to run the web app alone against an
already-running M4 endpoint).

### Demo commands

The router is deterministic (no free-form LLM): same input, same command.

- `Audit my AWS workspace` — runs `audit_workspace`
- `What resources need attention?` — evaluates and lists flagged resources
- `Why does web-assets-cdn matter?` / `Explain orphan-lambda-no-owner` —
  runs `explain_resource`
- `Show my pending approvals` — runs `list_approvals`
- `Approve this ticket demo-ticket-0001` / `Deny demo-ticket-0002` —
  runs `decide_ticket` (updates the in-memory store only)
- `help`, anything unrecognized — deterministic, no tool called

### M5 boundaries (what the demo is NOT)

- It is a **synthetic demo**: every figure is projected/illustrative, and the
  UI is labeled as such.
- `decide_ticket` only transitions the in-memory ticket store. No AWS action
  is ever executed, and SWS does not expose any action-execution tool.
- No auth, no TLS, no non-loopback exposure: designed to run on your own
  machine.
- The approval store is lost on restart (in-memory, seeded fresh each run).
- The UI is a plain HTML/JS single page with no framework.

### M5 tests

```bash
python -m pytest tests/test_simulator_router.py tests/test_simulator_demo.py \
  tests/test_simulator_service.py tests/test_simulator_app.py \
  tests/test_simulator_mcp.py -q
```

`test_simulator_mcp.py` starts the real M4 server under uvicorn on an
ephemeral loopback port and drives it with the official MCP client; the
remaining simulator suites are hermetic.

## M6: real AWS client factory

M6 is the smallest production integration that makes the existing S3, Lambda,
and Cost Explorer collectors usable through the M4 MCP backend against a real
AWS account. It is infrastructure plumbing only — no collector, policy, MCP,
or simulator behavior changed.

- `src/sws_agent/aws.py` adapts boto3 clients to the already-existing
  collector client protocols via a thin `AwsMultiClient` (exactly the six
  collector methods) and a lazy `AwsClientFactory`. boto3/botocore are
  imported only when a collector tool actually runs; importing `sws_agent`
  never does.
- The factory consumes the existing `AWSConnectionConfig` (region/profile)
  and applies the canonical `AWS_API_RETRY_ATTEMPTS` / `AWS_API_TIMEOUT_SECONDS`
  limits through `botocore.config.Config`.
- Hermetic behavior is unchanged: the default `SwsMcpServer` still carries no
  client factory, so inventory/cost tools fail with the same deterministic
  `ToolError("no AWS client factory configured for this server")`, and the
  whole test suite stays credential-free and never imports boto3.

### Running against a live account (opt-in, read-only)

```bash
pip install -e ".[dev,mcp,aws]"
$env:SWS_AWS_REGION="us-east-1"   # optional: target region
$env:SWS_AWS_PROFILE="default"    # optional: boto3 profile
python -m sws_agent.mcp.server
```

Only read-only permissions are exercised: `ListBuckets`, `GetBucketLocation`,
`GetBucketTagging`, `ListFunctions`, `ListTags`, `GetCostAndUsage`, and
optionally `sts:GetCallerIdentity`. Credentials always resolve through boto3's
standard chain; nothing is stored, logged, traced, or printed.

Known limitation: Lambda is a regional service, and the factory creates one
Lambda client for the configured region. Region lists passed to the workspace
collectors are preserved as snapshot metadata, but Lambda inventory is scanned
from the configured region only. Multi-region Lambda scanning requires
per-region client injection and is deliberately outside M6.

## M7: safe action-planning & authorization workflow

M7 makes the pre-execution authorization boundary reachable end-to-end:
`request_approval` maps a candidate (resource, action) through the existing
deterministic `ActionAuthorizer` and, whenever the gate requires human
approval, opens a `PENDING` ticket in the approval store. The simulator
`DemoBackend` exercises the same real planner, so the demo behaves exactly
as production does.

- The **execution mode** is an operator setting (`SWS_EXECUTION_MODE`,
  values `safe`/`review`/`autonomous`, validated through `SWSRuntimeConfig`
  — absent/blank means `safe`). It is fixed at backend construction; it is
  never a per-request client input, so callers cannot widen autonomy.
- `request_approval(resource_id, resource_type, action, rationale="")`
  returns `{"plan": {...}}` with the authorization decision and, when
  required, the PENDING ticket. Zero-side-effect actions (`leave`,
  `flag_for_review`) authorize without a ticket; `stop_resource` requires
  human approval in `safe`/`review` and authorizes in `autonomous`.
- M7 is strictly pre-execution: `ActionPlan.executed` is always `False`,
  there is no action executor, and no AWS call is ever made by the planner.
- Cost honesty add-on: the standalone `get_cost_estimates` tool now reports
  `truncated` and `failures` alongside `cost_estimates`, derived from the
  same trace semantics as workspace audit — a paged Cost Explorer response
  that hits the 20-page cap is marked truncated instead of silently sold as
  complete.

## M8: durable audit ledger + lineage

M8 adds persistence behind the existing read-only tools without changing any
tool request/response shape. Every domain fact gets a stable id, and —
when an audit store is configured — every successful tool result is appended
to a durable ledger.

- **Lineage ids**: `PolicyDecision` carries a deterministic `decision_id`
  (SHA-256 over snapshot id + resource id + rule, so re-evaluating a snapshot
  is a repeatable fact) plus `snapshot_id`/`run_id`; `ActionPlan` carries
  `action_plan_id` + `created_at`; `ApprovalTicket` carries an optional
  `plan_id`; every collection run owns a `run_id`. Existing constructions
  remain valid (all additive, with defaults).
- **Ledger** (`src/sws_agent/audit.py`): append-only UTF-8 JSONL, one
  `AuditEnvelope` per line (`schema_version`, stamped `record_id` +
  `created_at`, kind, correlation ids, reserved `execution`, payload). No
  SQLite, no rotation, no deletion. A duplicate `record_id` or a corrupt
  existing line fails fast; a `close()`d store refuses further writes.
  Sanitization: never `ResourceRecord.raw`, explanation prose, credentials,
  wire payloads, or raw Cost Explorer pages.
- **Write-through**: when an `audit_store` is injected (or `SWS_AUDIT_DIR`
  is set for `python -m sws_agent.mcp.server`), the backend records `RUN` +
  `SNAPSHOT` per collection, one `DECISION` per policy decision, `PLAN` +
  `TICKET` on `request_approval`, a `TICKET` on `decide_ticket`, `EXPLANATION`
  per explanation, and `RUN` + `COST` for standalone cost collection.
- **Fail-loud**: a persistence failure propagates as `ToolError` (via the
  `_guarded` seam). If a tool call itself fails, no durable record is written
  — SWS never claims durable facts it did not persist.
- **M8 is read-only persistence**: no `get_history` tool, no executor, no
  `execution` records (the envelope stanza is reserved for M9).

## Layout

```
src/sws_agent/
    constants.py     canonical enums, action vocabulary, and limits
    models.py        SWS domain models (pydantic)
    trace.py         execution-event trace recording
    interfaces.py    protocol boundaries between orchestration layers
    authorization.py deterministic authorization gate
    relationships.py deterministic-evidence-over-inference merging
    config.py        fail-fast configuration validation
    aws.py           real AWS client factory (M6, lazy optional boto3)
    audit.py         durable append-only JSONL audit ledger (M8)
    _identity.py     UTC-aware clock + id sources (stdlib-only, hermetic)
    mcp/             MCP boundary (real Streamable HTTP server, M4)
    simulator/       M5 web demo: demo backend, router, client, service, app
tests/               hermetic unit tests (no AWS credentials)
docs/                reuse decisions and current scope
scripts/experiments/ throwaway experiments (not committed)
```

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests -q
```

The test suite is fully hermetic: it never requires live AWS credentials and
never touches AWS. `test_simulator_mcp.py` is local-only (loopback ephemeral
port, no internet).

The M5 demo adds an optional extras group:

```bash
pip install -e ".[dev,mcp,simulator]"
```

## Repository safety boundary

- `D:\SMS` (Semantic Memory Steward) is a **frozen, read-only** architecture
  reference. SWS selects only documented, domain-agnostic patterns from it and
  never copies it wholesale.
- The SWS repository is completely independent: its own `.git`, its own
  history. Nothing is pushed without explicit authorization.