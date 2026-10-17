# Runbook: deploy the SWS public demo website (Render free)

- **Status**: deploy configuration is committed (`deploy/web/Dockerfile`,
  `render.yaml`) and the container is verified locally. **The Render service and
  the DNS record are manual operator steps** — this environment holds no Render
  credentials. Nothing here creates or changes any AWS resource.
- **What it deploys**: the existing M5 simulator, unchanged, as a self-contained
  **synthetic** demo. No AWS access, no credentials, no live AWS calls, no calls
  to `mcp.sjsujalgupta.xyz`, no mutation path.
- **Target**: `https://sws.sjsujalgupta.xyz`.
- **Cost**: `$0` AWS, `$0` AWS credits, `$0` Render (free plan, no card).
- **Authority / decision**: `docs/adr/0014-public-demo-website-hosting.md`.

---

## 0. Preconditions

1. The deploy config is on the default branch of
   `https://github.com/Synthesis-works/Semantic-Workspace-Steward` (`main`).
2. A **Render account** (GitHub sign-in) exists. No payment method is required for
   the free plan.
3. You can edit DNS for `sjsujalgupta.xyz` (the apex is on an **external**
   registrar, **not** AWS Route 53 — do not look for it there).

---

## 1. Local pre-deploy verification (do this first)

Build and run the image exactly as Render will, then smoke-test the routes.

```powershell
docker build -f deploy/web/Dockerfile -t sws-public-demo .
docker run --rm -p 8080:8080 -e SWS_SIM_HOST=0.0.0.0 -e PORT=8080 sws-public-demo
```

In another shell:

```powershell
curl.exe -sS -o NUL -w "%{http_code}`n" http://127.0.0.1:8080/          # expect 200
curl.exe -sS http://127.0.0.1:8080/api/meta                              # expect {"demo": true, ...}
curl.exe -sS -X POST http://127.0.0.1:8080/api/chat -H "content-type: application/json" -d '{\"message\":\"help\",\"history\":[]}'
curl.exe -sS http://127.0.0.1:8080/api/tools                            # expect 8 read-only tools, no execute/apply
```

The container log must show the web app binding `0.0.0.0:8080` (from `PORT`). Do
not proceed if the port is `127.0.0.1` or the port differs from `PORT`.

---

## 2. Create the Render service

**Preferred — Blueprint.** In Render: **New → Blueprint**, select this repo and
branch `main`. Render reads `render.yaml` and provisions one free Docker web
service named `sws-public-demo` with the custom domain attached.

**Manual alternative.** **New → Web Service** → Docker → this repo/branch, then:

| Field | Value |
| --- | --- |
| Name | `sws-public-demo` |
| Region | any (default) |
| Dockerfile path | `deploy/web/Dockerfile` |
| Docker build context | repo root (`.`) |
| Instance type | **Free** |
| Environment variable | `SWS_SIM_HOST` = `0.0.0.0` |

Do **not** set a port or add a health-check path: the app takes Render's injected
`PORT` and there is deliberately no `/healthz`.

---

## 3. Verify the default URL

Wait for the first deploy to go live. Render will show a default URL such as
`https://sws-public-demo.onrender.com`. Verify it:

```powershell
curl.exe -sS -o NUL -w "%{http_code}`n" https://sws-public-demo.onrender.com/
```

Expect `200` and the "SWS interactive demo" UI. The first request may take ~1
minute if the free instance was asleep.

---

## 4. Attach the custom domain

If you used the Blueprint, the domain is already added — skip to step 5. Otherwise:
**Service → Settings → Custom Domains → Add** `sws.sjsujalgupta.xyz`. Render shows
a DNS target, e.g. `sws-public-demo.onrender.com`.

---

## 5. Add the DNS record (external registrar)

At the DNS provider for **`sjsujalgupta.xyz`** (not Route 53), add:

| Type | Name | Value | TTL |
| --- | --- | --- | --- |
| `CNAME` | `sws` | `sws-public-demo.onrender.com` (the target Render shows) | auto / 300 |

This is the **only** DNS change, and it is made on the external apex. Do not touch
`mcp.sjsujalgupta.xyz` or any AWS record.

---

## 6. Verify the public site

Once Render reports the certificate issued (minutes after DNS resolves):

```powershell
curl.exe -sS -o NUL -w "%{http_code}`n" https://sws.sjsujalgupta.xyz/
curl.exe -sS https://sws.sjsujalgupta.xyz/api/meta
```

Expect `200` and `{"demo": true, ...}` over HTTPS. Then confirm the separate MCP
deployment is unaffected:

```powershell
curl.exe -sS -o NUL -w "%{http_code}`n" https://mcp.sjsujalgupta.xyz/mcp
```

Report `https://sws.sjsujalgupta.xyz` as live **only** after these succeed.

---

## 7. Teardown / rollback

- **Suspend/delete**: in Render, delete the `sws-public-demo` service. Then remove
  the `sws` CNAME at the registrar.
- **Redeploy**: push a new commit to `main` (`autoDeploy` is on), or use
  **Manual Deploy → Deploy latest commit**.
- No AWS resource is ever created by this runbook, so there is nothing to clean up
  on AWS.

---

## What this runbook must never do

- Create, modify, or delete any AWS resource.
- Modify the M17 ECS/AgentCore deployment, the `mcp.sjsujalgupta.xyz` certificate,
  or the `Z03143173W3VFUCOJNOOD` private hosted zone.
- Introduce AWS credentials, live AWS calls, or a mutation path into the demo.
- Add a health endpoint or an authentication layer that the demo does not have.
