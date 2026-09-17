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
| `pyproject.toml` | src-layout packaging, dev extra, editing-based tooling | Standard practice | Minimal dependency set (only `pydantic` runtime + `pytest` dev); `boto3` and MCP SDK deferred until genuinely implemented |

## Intentionally rewritten (not copied)

- **Action vocabulary** — SWS uses `LEAVE / FLAG_FOR_REVIEW / REQUEST_APPROVAL / STOP_RESOURCE` for AWS resources. SMS's file-lifecycle states (KEEP / ARCHIVE / TRASH / QUARANTINE) are deliberately banned; `constants.SMS_DEPRECATED_ACTIONS` guards this in tests.
- **Domain models** — all `models.py` records (ResourceRecord, PolicyDecision, AuthorizationRequest, CostEstimate, ...) are new.
- **Policy engine** — SMS's retention heuristics are not portable. Only the `PolicyEngine` protocol (interface) exists so far; the deterministic implementation is future work.
- **Semantic layer, embeddings, provider adapters** — SMS-specific and excluded.
- **Web simulator / dashboard** — SWS does not adopt the Streamlit dashboard; the eventual interactive layer will talk to the real MCP server.

## Intentionally excluded from SWS

- SMS's file-lifecycle policy states and policy engine
- Streamlit dashboard and UI state
- Local filesystem inventory and document models
- Document-content ingestion and Comprehend processing
- S3 Vectors / embedding architecture and provider fallbacks
- Legacy CLI entry points and root-level debug scripts
- SMS dependencies not needed now (pypdf, openpyxl, strands-agents, streamlit, pandas, altair)

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