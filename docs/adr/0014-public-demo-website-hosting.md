# ADR 0014: Public demo website hosting (free external host, synthetic-only)

- Status: **accepted.** The deploy configuration is committed and the container
  is verified locally. **No AWS resource is created or modified by this ADR, and
  the M17 MCP/AgentCore deployment is untouched.** The actual Render service and
  the DNS CNAME are manual operator steps (this environment holds no Render
  credentials); see `docs/runbooks/deploy-public-demo-render.md`.
- Milestone: public demo website (post-M17 deployment task)
- Date: 2026-10-17
- Depends on: M5 (the local starlette simulator, `sws_agent.simulator`), M4 (the
  real MCP server the demo drives over its tool boundary), ADR 0013 (the separate
  AgentCore Harness deployment this ADR must not touch).

## Context

SWS needs a public website at `https://sws.sjsujalgupta.xyz`. The user imposed a
hard budget: the website must cost **$0** and consume **$0 AWS credits**. It must
not be deployed on ECS/ALB, App Runner, or any AWS hosting; no new AWS resources
may be created and existing AWS infra must not change except, at most, DNS. The
existing MCP/AgentCore deployment (`https://mcp.sjsujalgupta.xyz`) must stay
completely separate and unchanged.

The UI that already exists is the **M5 simulator** (`sws_agent.simulator`): a
starlette ASGI web app served by uvicorn (`python -m sws_agent.simulator`). It is
deliberately **synthetic-only** — it drives the real M4 MCP tool boundary over an
in-process `DemoBackend` (a fixed, seeded workspace), so it makes no AWS calls and
needs no credentials. Its only non-determinism is in-memory, per-run session state
that is re-seeded on every start.

Two properties decided the hosting choice:

1. The app is a **long-lived, stateful process**: `__main__` starts *two* uvicorn
   servers (an MCP server on loopback `127.0.0.1:8765` and the web app), and the
   web app opens a **persistent MCP client session** at startup. It is not a
   static site and not a request-scoped function.
2. It binds `127.0.0.1` and reads `SWS_SIM_HOST`/`SWS_SIM_PORT`, with no platform
   port support.

## Decision

Host the **existing demo, unchanged**, on a **Render free Web Service (Docker)**.

- **External free host, not AWS.** Render's free plan runs a persistent Docker
  container with a free custom domain and managed TLS, at $0 and with no credit
  card. This satisfies the budget and keeps the workload off AWS entirely.
- **The app is reused, not re-architected.** The image installs only the
  `simulator` extra (starlette + MCP SDK + uvicorn + httpx) and runs the unchanged
  entrypoint. The HTML/CSS/JS UI and `Simulator` service are untouched.
- **One minimal production change.** `sws_agent.simulator.__main__._resolve_web_port`
  now resolves the web port as `SWS_SIM_PORT` → platform `PORT` → `8080`, so the
  container binds the host-injected port. Local behavior is unchanged.
- **Synthetic-only by construction.** The image contains no boto3, no AWS
  credentials, and no code path to AWS or to `mcp.sjsujalgupta.xyz`. `decide_ticket`
  remains excluded from the agent tool catalog.
- **Separation is preserved.** Nothing in this ADR reads, writes, or references the
  M17 ECS/AgentCore/Route 53 resources. The only operator action touching DNS is a
  CNAME at the external registrar for `sjsujalgupta.xyz`.

### Rejected alternatives

- **Vercel Hobby.** Vercel runs Python as request-scoped serverless functions
  loading an `app` variable; it does not run `python -m sws_agent.simulator` and
  cannot host the app's loopback MCP server or its persistent client session.
  Making it fit would require an in-process client adapter + a serverless
  entrypoint — a real architectural change, and Hobby is also non-commercial-only.
- **Google Cloud Run.** Viable but requires a GCP billing account and uses GCP, not
  AWS; chosen against in favor of Render's no-card free tier.
- **Any AWS hosting.** Excluded by the hard budget constraint.

## What this ADR does **not** do

- No health endpoint (`/healthz` or otherwise) is added — an explicit operator
  decision.
- No AWS resource is created or modified; no IAM, ECS, ECR, ALB, or Route 53 change.
- The M17 MCP/AgentCore deployment and the `mcp.sjsujalgupta.xyz` certificate are
  untouched.
- No authentication is added to the demo (it has none today, and it serves only
  synthetic data with no mutation path).
- No change to the SWS execution semantics, the nine-tool MCP boundary, or any
  frozen v1 invariant.

## Consequences

- **Cold starts.** Render's free plan spins the service down after ~15 minutes of
  inactivity; the next request pays a ~1 minute cold start. Acceptable for a demo.
- **In-memory state resets** when the instance restarts. The demo re-seeds its
  synthetic workspace on every start, so this is by design, not data loss.
- **Single free instance**, no persistent disk — neither is needed.
- **Platform terms.** The deployment remains subject to Render's acceptable-use
  terms; the manifest pins the free plan and the region default.

## Verification

- `deploy/web/Dockerfile` builds and runs locally; `GET /` returns the UI and the
  existing `/api/*` routes respond (see the runbook).
- The full hermetic test suite and `ruff` pass, including a new hermetic test for
  the port-resolution precedence in `tests/test_simulator_main.py`.
- `https://sws.sjsujalgupta.xyz` and `https://mcp.sjsujalgupta.xyz` are verified
  **after** the manual Render + DNS steps, and only then reported as live.
