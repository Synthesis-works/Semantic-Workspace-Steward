# Runbook: deploy the SWS public demo website (Render free)

- **Status**: deploy configuration is committed (`deploy/web/Dockerfile`,
  `render.yaml`). **The Render service, its secret credentials, and the DNS
  record are manual operator steps** — this environment holds no Render
  credentials. Nothing here creates or changes any AWS resource.
- **What it deploys**: the existing M5 simulator, unchanged, as a self-contained
  **synthetic** demo whose SWS workspace is synthetic (`DemoBackend` behind an
  in-process MCP server). The conversational layer is the **real Strands +
  Amazon Bedrock** provider, restricted to a hard-coded read-only tool catalog.
  No AWS resource API is reachable, no live inventory, no calls to
  `mcp.sjsujalgupta.xyz`, no mutation path.
- **Target**: `https://sws.sjsujalgupta.xyz`.
- **Cost**: `$0` Render (free plan, no card). AWS is used **only** for Bedrock
  inference with `amazon.nova-lite-v1:0` (bounded — see §7).
- **Authority / decision**: `docs/adr/0014-public-demo-website-hosting.md` and
  `docs/adr/0015-public-demo-conversational-llm.md`.

---

## 0. Preconditions

1. The deploy config is on the default branch of
   `https://github.com/Synthesis-works/Semantic-Workspace-Steward` (`main`).
2. A **Render account** (GitHub sign-in) exists. No payment method is required for
   the free plan.
3. You can edit DNS for `sjsujalgupta.xyz` (the apex is on an **external**
   registrar, **not** AWS Route 53 — do not look for it there).
4. You have a **Bedrock-only IAM identity** for the demo (§1). Do **not** reuse
   the SWS execution role or any operator/admin credentials.

---

## 1. Create the Bedrock-only IAM identity (manual, one-time)

> This is an AWS IAM action performed **manually by an operator**. It is not run
> by the coding agent and creates no resource other than this small identity.
> It must grant **only** Bedrock inference on the single chosen model.

Minimum policy (attach to a dedicated user or role — never a broad role):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeNovaLiteOnly",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"
      ]
    }
  ]
}
```

This identity must **not** have `ec2:*`, `s3:*`, `lambda:*`, `cloudtrail:*`,
`ce:*`, `sts:AssumeRole`, `iam:*`, `ssm:*`, or any mutation permission, and must
not be able to reach the SWS MCP endpoint. The LLM infrastructure identity is
**not** the SWS execution identity.

For a long-lived service, prefer an IAM role scoped to the deployment; Render's
free tier has no instance-role mechanism, so a dedicated IAM **user** with an
access key stored as Render secrets (§3) is the pragmatic choice — treat the
key as demo-only and rotate/delete it when the demo is retired.

---

## 2. Local pre-deploy verification (do this first)

Build and run the image exactly as Render will.

**2a. Deterministic fallback (no credentials):** confirms the synthetic demo and
the honest "deterministic" mode when no model is configured.

```powershell
docker build -f deploy/web/Dockerfile -t sws-public-demo .
docker run --rm -p 8080:8080 -e SWS_SIM_HOST=0.0.0.0 -e PORT=8080 sws-public-demo
```

In another shell:

```powershell
curl.exe -sS http://127.0.0.1:8080/api/meta   # expect mode "deterministic"
curl.exe -sS -X POST http://127.0.0.1:8080/api/chat -H "content-type: application/json" -d '{\"message\":\"help\",\"history\":[]}'
```

The container log must show the web app binding `0.0.0.0:8080` (from `PORT`). Do
not proceed if the port is `127.0.0.1` or differs from `PORT`.

**2b. Conversational (real Bedrock):** pass the demo's Bedrock-only credentials
and the provider config. `/api/meta` must report `mode: "llm"`, `provider:
"strands"`, `model: "amazon.nova-lite-v1:0"`.

```powershell
docker run --rm -p 8080:8080 \
  -e SWS_SIM_HOST=0.0.0.0 -e PORT=8080 \
  -e SWS_AGENT_PROVIDER=strands -e SWS_AGENT_MODEL=amazon.nova-lite-v1:0 -e AWS_REGION=us-east-1 \
  -e AWS_ACCESS_KEY_ID=$env:AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY=$env:AWS_SECRET_ACCESS_KEY \
  sws-public-demo
```

```powershell
curl.exe -sS http://127.0.0.1:8080/api/meta
curl.exe -sS http://127.0.0.1:8080/api/tools            # expect 8 read-only tools, no execute/apply
curl.exe -sS -X POST http://127.0.0.1:8080/api/chat -H "content-type: application/json" -d '{\"message\":\"Audit my workspace\",\"history\":[]}'
```

Confirm the MCP trace shows only allowed synthetic tools. If credentials are
absent/unavailable, the response must say the conversational provider is
unavailable — never pretend a model answered.

---

## 3. Create the Render service

**Preferred — Blueprint.** In Render: **New → Blueprint**, select this repo and
branch `main`. Render reads `render.yaml` and provisions one free Docker web
service named `sws-public-demo` with the custom domain and the non-secret env
vars (`SWS_SIM_HOST`, `SWS_AGENT_PROVIDER`, `SWS_AGENT_MODEL`, `AWS_REGION`)
attached.

**Manual alternative.** **New → Web Service** → Docker → this repo/branch, then:

| Field | Value |
| --- | --- |
| Name | `sws-public-demo` |
| Region | any (default) |
| Dockerfile path | `deploy/web/Dockerfile` |
| Docker build context | repo root (`.`) |
| Instance type | **Free** |
| Env var | `SWS_SIM_HOST` = `0.0.0.0` |
| Env var | `SWS_AGENT_PROVIDER` = `strands` |
| Env var | `SWS_AGENT_MODEL` = `amazon.nova-lite-v1:0` |
| Env var | `AWS_REGION` = `us-east-1` |

**Secret env vars (Render → Environment → Secret Files / Environment Variables;
mark as secrets).** These are the Bedrock-only identity's credentials from §1 and
must **never** be committed or baked into an image:

| Key | Value |
| --- | --- |
| `AWS_ACCESS_KEY_ID` | the demo Bedrock-only user's access key id |
| `AWS_SECRET_ACCESS_KEY` | the demo Bedrock-only user's secret access key |

Do **not** set `SWS_MCP_URL`, do not add the MCP API key, and do not set a port
or a health-check path: the app takes Render's injected `PORT` and there is
deliberately no `/healthz`.

---

## 4. Verify the default URL

Wait for the first deploy to go live. Render will show a default URL such as
`https://sws-public-demo.onrender.com`. Verify it:

```powershell
curl.exe -sS -o NUL -w "%{http_code}`n" https://sws-public-demo.onrender.com/
curl.exe -sS https://sws-public-demo.onrender.com/api/meta   # expect mode "llm", provider "strands"
```

Expect `200` and the "SWS" conversational UI. The first request may take ~1
minute if the free instance was asleep.

---

## 5. Attach the custom domain

If you used the Blueprint, the domain is already added — skip to step 6. Otherwise:
**Service → Settings → Custom Domains → Add** `sws.sjsujalgupta.xyz`. Render shows
a DNS target, e.g. `sws-public-demo.onrender.com`.

---

## 6. Add the DNS record (external registrar)

At the DNS provider for **`sjsujalgupta.xyz`** (not Route 53), add:

| Type | Name | Value | TTL |
| --- | --- | --- | --- |
| `CNAME` | `sws` | `sws-public-demo.onrender.com` (the target Render shows) | auto / 300 |

This is the **only** DNS change, and it is made on the external apex. Do not touch
`mcp.sjsujalgupta.xyz` or any AWS record.

---

## 7. Verify the public site + cost guardrails

Once Render reports the certificate issued (minutes after DNS resolves):

```powershell
curl.exe -sS -o NUL -w "%{http_code}`n" https://sws.sjsujalgupta.xyz/
curl.exe -sS https://sws.sjsujalgupta.xyz/api/meta
```

Expect `200` and `{"demo": true, "mode": "llm", "provider": "strands", ...}`
over HTTPS. Then confirm the separate MCP deployment is unaffected:

```powershell
curl.exe -sS -o NUL -w "%{http_code}`n" https://mcp.sjsujalgupta.xyz/mcp
```

Cost guardrails that are enforced in code (do not loosen without an ADR):

- Nova Lite only; `AGENT_MAX_TOKENS = 768`; loop bounded at `AGENT_MAX_STEPS = 6`;
  history capped at `AGENT_HISTORY_LIMIT = 16`; no retries, no streaming, no
  background/scheduled inference, no RAG/embeddings/memory.
- One bounded inference workflow per user message.
- If a Bedrock call fails, it degrades once (no retry storm).

Report `https://sws.sjsujalgupta.xyz` as live **only** after these succeed.

---

## 8. Teardown / rollback

- **Suspend/delete**: in Render, delete the `sws-public-demo` service. Then remove
  the `sws` CNAME at the registrar, and **delete the Bedrock-only IAM identity /
  access key** from §1.
- **Disable the LLM only**: clear `SWS_AGENT_MODEL` (or remove
  `SWS_AGENT_PROVIDER`) in Render — the demo falls back to deterministic mode and
  makes **no** Bedrock calls.
- **Redeploy**: push a new commit to `main` (`autoDeploy` is on), or use
  **Manual Deploy → Deploy latest commit**.

---

## What this runbook must never do

- Create, modify, or delete any AWS resource other than the dedicated
  **Bedrock-only demo identity** in §1.
- Grant that identity anything beyond Bedrock model invocation (no
  `ec2:*`/`s3:*`/`lambda:*`/`cloudtrail:*`/`ce:*`/`sts:AssumeRole`/`iam:*`).
- Modify the M17 ECS/AgentCore deployment, the `mcp.sjsujalgupta.xyz` certificate,
  or the `Z03143173W3VFUCOJNOOD` private hosted zone.
- Point the demo at `mcp.sjsujalgupta.xyz`, add the MCP API key, or expose a
  mutation path / an execution tool.
- Commit AWS credentials or bake them into the image.
- Add a health endpoint or an authentication layer that the demo does not have.
