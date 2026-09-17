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

This is the **foundation** commit. It establishes clean interfaces and project
boundaries so capabilities can be added incrementally. It does **not** yet:

- deploy or create AWS infrastructure
- run AWS API calls or collectors
- implement destructive actions or the full action engine
- implement inventory collectors, the policy engine, or the web simulator
- wire the real MCP SDK server

Deliberately deferred (documented in `docs/reuse-decisions.md`): S3 inventory,
EC2/EBS or Lambda inventory, Cost Explorer analysis, the semantic layer, and
the actual MCP server over Streamable HTTP.

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
An actual MCP SDK server on top of the tested `ToolRegistry` is future work;
no MCP compliance is claimed until it is genuinely wired, invoked, and tested.

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
    mcp/             MCP boundary (dependency-free tool registry)
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
never touches AWS.

## Repository safety boundary

- `D:\SMS` (Semantic Memory Steward) is a **frozen, read-only** architecture
  reference. SWS selects only documented, domain-agnostic patterns from it and
  never copies it wholesale.
- The SWS repository is completely independent: its own `.git`, its own
  history, and **no remote configured**. Nothing is pushed without explicit
  authorization.