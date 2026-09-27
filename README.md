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
  (`src/sws_agent/mcp/server.py`) exposing **eight tools**
  (`audit_workspace`, `collect_workspace`, `get_relationships`,
  `evaluate_workspace`, `get_cost_estimates`, `explain_resource`,
  `list_approvals`, `decide_ticket`) over a thin `SwsBackend` seam. It is
  wired, invoked, and tested against the official MCP SDK v2 client. AWS
  collectors are still injected via a backend, never hard-wired.
- **M5** — a local, demo-only **web simulator** (below) that talks to the
  real M4 MCP server through the official MCP client.

It does **not** yet:

- deploy or create AWS infrastructure
- run AWS API calls or collectors against a live account
- implement destructive actions or the full action engine
- expose the simulator or MCP server on anything but local loopback

Deliberately deferred (documented in `docs/reuse-decisions.md`): S3/EC2/EBS
inventory collectors, Cost Explorer analysis, AWS auth, and non-loopback
deployment. Nothing in this repository ever fabricates AWS usage, savings,
or history.

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