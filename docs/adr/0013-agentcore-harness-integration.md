# ADR 0013: AgentCore Harness integration (design, hermetic validation, deployment)

- Status: **accepted for design + hermetic validation.** The integration
  configuration, the deployed-endpoint design, and the endpoint's opt-in
  authentication gate are in scope. **No AWS resource is created and nothing is
  deployed by this ADR.** Every deployed-design element below is either
  verified against current AgentCore docs / the installed SDK or explicitly
  listed as a deploy-time verification item.
- Milestone: M17 (AgentCore Harness integration)
- Date: 2026-10-15
- Depends on: M4 (real MCP server over Streamable HTTP), the M5 optional
  conversational layer (`sws_agent.simulator.agent`), ADR-free but grounded in
  `docs/reuse-decisions.md` ("the AgentCore Harness consumes this server via its
  remote MCP integration").

## Context

SWS exposes a real MCP server (`sws_agent.mcp.server.SwsMcpServer`) on the
Streamable HTTP transport with **nine** tools. The MCP server docstring and
`docs/reuse-decisions.md` already record the intended next surface: an
**AgentCore Harness** that consumes this server through its remote MCP
integration, with authentication and deployment deferred to that later step.

AgentCore Harness is a **managed Strands agent loop**: the harness runs the
reasoning -> tool-call -> result -> response loop, and SWS supplies only
configuration (model, system prompt, tools, limits). It is a hosting/runtime
surface. The open question this ADR answers is whether the agent can run through
AgentCore **without weakening any existing boundary**.

Two facts constrain the answer:

1. The SWS MCP server currently binds **loopback only, with no TLS and no
   authentication** (`main()` defaults to `127.0.0.1:8000`). A Harness
   `remote_mcp` tool requires an **HTTPS** URL. Making the server remotely
   reachable is therefore a genuine infrastructure step, not a config change.
2. The local conversational path interposes a **deterministic SWS validation**
   step: the model proposes a tool call and SWS code validates name/arguments
   before any MCP call (`validate_agent_tool_call`). On the Harness path the
   managed loop calls the remote MCP tools **directly**, so that interposed
   validator is absent and must be replaced by an equivalent control.

## Decision

Run the SWS agent through an **AgentCore Harness** whose **only** tool is the
existing SWS MCP server, added as a single `remote_mcp` entry:

- **One tool, remote MCP, HTTPS.** The harness declares exactly one tool of type
  `remote_mcp` pointing at the SWS endpoint. No managed-loop built-in
  (`shell`, `file_operations`) and no other AgentCore tool (Browser, Code
  Interpreter, Gateway) is declared.
- **Allowlist = the read-only SWS catalog.** `allowedTools` is exactly the eight
  read-only tools the local agent already uses, **derived from**
  `sws_agent.simulator.agent.AGENT_TOOL_SCHEMAS` (the single source of truth) and
  excluding `decide_ticket`. There is no second catalog.
- **Memory disabled.** `memory: {"disabled": {}}` -- no new persistent memory.
- **Explicit limits.** `maxIterations`, `maxTokens`, `timeoutSeconds`, and the
  model's `maxTokens` are all set explicitly, never defaulted (cost/abuse guard).
- **Nothing else.** No new tool, no new MCP implementation, no direct boto3, no
  autonomous execution, no new approval semantics, no new agent/subagent.

The builder (`sws_agent.agentcore_harness.build_harness_config`) is
deterministic, stdlib-only, creates no resource, opens no socket, and imports no
AWS SDK.

### The control that replaces the local validator

Because the managed loop calls MCP tools directly, the boundary is preserved by
**two** controls, both of which must hold:

1. the Harness `allowedTools` allowlist (this ADR) -- excludes `decide_ticket`;
   and
2. the SWS MCP server's own deterministic schema validation and `ToolError`
   fail-closed boundary (unchanged).

No execution/mutation tool exists anywhere on the MCP surface, so even a fully
compromised loop can at most invoke one of the eight read-only tools.

### Endpoint authentication (the harness -> endpoint half)

The harness reaches the endpoint as a single `remote_mcp` tool. That tool has
exactly two fields (`url`, `headers`), and `headers` is the **only** outbound
authentication mechanism available to a remote MCP server: the SigV4/OAuth
`outboundAuth` options belong to `agentCoreGateway`, which is explicitly out of
scope. The configuration therefore declares one header whose value is a
**credential-provider reference**:

```json
{"remoteMcp": {"url": "https://<endpoint>/mcp",
               "headers": {"x-api-key": "${arn:aws:bedrock-agentcore:<region>:<account>:token-vault/default/apikeycredentialprovider/<name>}"}}}
```

AgentCore resolves that reference to the real API key at invocation time, so no
secret is ever stored in harness configuration. The builder enforces this: the
only accepted header values are whole-value `${arn:...}` references to a
token-vault API-key credential provider (`CREDENTIAL_HEADER_VALUE_PATTERN`);
literal values -- including `Bearer <token>` -- are refused, and reserved
hop-by-hop / MCP protocol headers cannot be overridden.

The **same secret** backs both halves. The credential provider is created with
`--api-key-secret-source EXTERNAL` and `--api-key-secret-config
{secretId=<arn>,jsonKey=api_key}` pointing at one AWS Secrets Manager secret;
the endpoint reads that secret and requires the presented header to match it.
One secret, two consumers, no duplication.

## Deployed endpoint (designed here, created only by a later authorized step)

### A. Architecture

```
AgentCore Harness session (Firecracker microVM, networkMode VPC)
   |  HTTPS + x-api-key (credential-provider reference)
   v
Internal NLB  (TLS listener, ACM cert, TCP target group, TCP health check)
   |  TCP 8000, allowed only from the harness egress SG
   v
ECS Fargate service (single task) running `python -m sws_agent.mcp.server`
   uvicorn on 0.0.0.0:8000, path /mcp, default DemoBackend
```

- The MCP server stays **unmodified in behaviour**: one Fargate task runs the
  existing entry point. No container is built by this ADR (no Dockerfile
  exists); the image is a deploy-milestone artifact.
- A **single task** because the transport keeps per-session state
  (`Mcp-Session-Id`); more than one task would need session affinity or
  stateless mode, and stateless mode would be a server change that is **not**
  made here.
- The NLB is **internal**, L4, and its listener is TLS. Because it is L4, the
  `Host` header is untouched and no HTTP header filtering is possible; access
  control is the security group plus the application gate.
- The endpoint's certificate must name a DNS record we control: an internal
  NLB's own `*.elb.amazonaws.com` name cannot be covered by an ACM
  certificate, so the design requires a domain with public DNS (for ACM
  validation) and a Route 53 **private** hosted zone alias for private
  resolution.

### B. Endpoint authentication (implemented, opt-in)

`sws_agent.mcp.endpoint_auth` wraps the ASGI app with a credential check. It is
**opt-in**: with `SWS_MCP_API_KEY_SECRET_ARN` unset, `gate()` returns the app
unchanged, so local development, the hermetic suite, and every existing
deployment are untouched. When set:

- the API key is read **once at startup** from that Secrets Manager secret
  (JSON field `api_key`); the reader is the only place boto3 is imported;
- every HTTP request must present the key in `SWS_MCP_AUTH_HEADER` (default
  `x-api-key`), compared with `hmac.compare_digest`;
- a missing or wrong value is answered `401` with a fixed body that reflects
  nothing from the request; the expected value is never logged or echoed;
- **fail closed**: secret unreadable, not JSON, key missing, value blank, or an
  invalid configured header name aborts startup. There is no fallback.

Authentication is not authorization: the gate answers only "is this the caller
we configured?". The single authorization boundary remains the deterministic
SWS core plus the allowlist.

### C. IAM separation (three roles, least privilege)

1. **Harness execution role** -- trust `bedrock-agentcore.amazonaws.com`; grants
   Bedrock inference for the chosen model, private ECR pull for
   `harness-<region>`, CloudWatch Logs, and credential resolution
   (`bedrock-agentcore:GetResourceApiKey` on the vault + API-key provider, and
   `secretsmanager:GetSecretValue` on
   `bedrock-agentcore-identity!default/apikey/<name>-*`). It is **not** granted
   `bedrock-agentcore:InvokeAgentRuntimeCommand` (which would allow command
   execution around the model), Browser/CodeInterpreter/Memory, or
   `sts:AssumeRole`.
2. **Caller role** -- `bedrock-agentcore:InvokeHarness` +
   `bedrock-agentcore:InvokeAgentRuntime` on the harness ARN, and
   `iam:PassRole` for the harness role only.
3. **Endpoint roles** -- an ECS **task execution role** (ECR pull + logs) and an
   ECS **task role** granted only `secretsmanager:GetSecretValue` on the one
   secret. The SWS mutation module (`ec2_mutation.py`) is not on the MCP import
   path, so no write permission is needed or granted.

### D. Transport behaviour (measured against the installed SDK, mcp 2.1.1)

Verified by running the real application on loopback:

- **Host header.** The SDK auto-enables DNS-rebinding protection **only** when
  the app is built for a loopback host. Built with the default (`127.0.0.1`) a
  request carrying a load balancer `Host` is answered **421 Invalid Host
  header**; built for `0.0.0.0` (required in a container anyway) the same
  request is accepted. The deployed entry point therefore binds `0.0.0.0` and
  leaves the SDK check off, with the security group and the credential gate
  carrying the load. Restricting `Host` to the endpoint name instead is
  possible but would require new configuration plumbing and is **not** done
  here.
- **Accept header.** `Accept: */*` is accepted (the SDK treats the wildcard as
  covering both JSON and SSE), so the known strict-Accept incompatibility does
  not apply.
- **Sessions.** Stateful; a bare `GET /mcp` is `400`, and `POST` without
  `content-type: application/json` is `400`.
- **Health checks.** There is **no 2xx probe path** (`/` and `/healthz` are
  `404`), so an HTTP health check would require a new route. An NLB **TCP**
  health check needs no server change and is the reason the NLB was chosen over
  an ALB here. An NLB is L4 and has no idle timeout to align with SSE.

### E. Rejected alternatives

- **API Gateway.** The ≤29 s integration timeout breaks the live-mode Bedrock
  calls, and HTTP APIs have no native API-key authorization.
- **Application Load Balancer.** No 2xx health-check path exists, so it would
  require adding a `/healthz` route; it also has an idle timeout to tune
  against SSE.
- **App Runner.** Private-ingress and streaming semantics unverified, and its
  basic auth would put a secret in configuration.
- **ALB header-rule auth.** Duplicates the secret into rule configuration and
  offers no constant-time comparison.
- **mTLS / SigV4 to the endpoint.** Not expressible by a `remote_mcp` tool.

### F. Prerequisites checklist (deploy milestone)

- VPC, ≥2 private subnets, and three security groups (harness egress, NLB,
  ECS task; the task SG allows 8000 only from the NLB SG, the NLB SG allows 443
  only from the harness SG -- the NLB SG must be attached **at NLB creation**,
  as it cannot be added later).
- Interface VPC endpoints `ecr.dkr`, `ecr.api`, `bedrock-runtime`, and an S3
  gateway endpoint (private subnets, no NAT required). The harness execution
  role needs private-ECR pull rights for `harness-<region>`.
- Secrets Manager secret holding `{"api_key": "..."}`; then
  `aws bedrock-agentcore-control create-api-key-credential-provider --name ...
  --api-key-secret-source EXTERNAL --api-key-secret-config
  {secretId=...,jsonKey=api_key}`.
- A domain with public DNS for ACM validation, an ACM certificate, and a
  Route 53 private hosted zone alias record to the NLB.
- ECR repository + image (not created by this ADR), ECS cluster/role/task
  definition/service (single task), internal NLB created with its SG + TLS
  listener + TCP target group + TCP health check.
- The two IAM policies of section C.
- `create-harness` with the builder's emitted body, including
  `networkMode: VPC` and the harness security group/subnets.

### G. Deploy-time verification items (no guessing)

1. **VPC field spelling.** The installed CLI help spells the nested networking
   object `networkModeConfig` (`securityGroups`/`subnets`) while the developer
   guide example spells `vpcConfig` (`securityGroupIds`/`subnetIds`). Resolve
   against the live API before deploy.
2. **Built-in tools.** Confirm `shell`/`file_operations` are excluded by the
   explicit `allowedTools` list, and add an explicit denial if not.
3. **Header delivery on every request.** Confirm the harness client sends the
   configured header on `initialize`, `POST`, and `GET`, not only the first
   call.
4. **Credential-provider reference form.** Confirm the whole-value `${arn:...}`
   reference is accepted for `remoteMcp.headers` (the documented form) and that
   `x-api-key` is the header name the client sends unchanged.
5. **Confused-deputy conditions.** Decide whether to add `aws:SourceArn` /
   `aws:SourceAccount` conditions to the harness role's trust policy (docs show
   a plain trust).
6. **Region availability** for AgentCore Harness.

## What this is not

- **Not an authorization boundary.** The deterministic SWS core (policy,
  authorizer, approval store) remains the sole authority. The harness decides
  only *which* read-only tool to call.
- **Not a second MCP server.** The transport is the existing `SwsMcpServer`; the
  harness consumes it as a URL.
- **Not a deployment.** The design above creates no harness, role, endpoint, or
  network. The separately authorized M17 deployment that realizes it is recorded
  under "Deployed realization (M17, 2026-10-16)" below.

## Open items for the (separately authorized) deploy milestone

These were deliberately unresolved when the design was written because they
require live AWS confirmation and create real resources. **The M17 deployment of
2026-10-16 resolved them against the live account**, as recorded under "Deployed
realization": the VPC field is spelled
`environment.agentCoreRuntimeEnvironment.networkConfiguration.networkModeConfig`
(§G.1); the harness role's trust policy carries
`aws:SourceAccount: 527557823928` (§G.5); AgentCore Harness is available in
`us-east-1` (§G.6); built-in tools are excluded by the explicit 16-entry
allowlist (§G.2); the harness sends the `${arn:...}` reference on `initialize`
and the gate answers `401` without the key and completes the handshake with it
(§G.3, §G.4); and AgentCore surfaces the remote tool as `sws_<name>` (observed at
invoke: `sws_list_approvals`), which the builder's dual `@sws/<name>` + bare
`<name>` entries cover without widening the tool set (item 2).

The original wording is retained for the record:

1. **MCP endpoint reachability and auth.** *Resolved in design.* The harness
   authenticates with a `remoteMcp.headers` credential-provider reference
   (`${arn:...}`) backed by one Secrets Manager secret, and the endpoint
   validates that secret in an opt-in ASGI gate (§B). Reachability is a
   VPC-placed Fargate task behind an internal NLB (§A). Remaining unknowns are
   listed in §G.
2. **Remote tool naming.** Confirm whether AgentCore namespaces remote MCP tool
   names when evaluating `allowedTools`; if it does, the allowlist names are
   adjusted to match (the set of *tools* is fixed and asserted here). The
   builder emits both `@sws/<name>` and the bare `<name>` for each read-only
   tool precisely so either convention matches without widening the set.
3. **Built-in tool presence.** See §G.2.
4. **IAM.** The three-role least-privilege split is specified in §C; exact
   policy documents are produced at deploy.
5. **Exact `create-harness` CLI flags** for the union fields (`model`, `tools`,
   `systemPrompt`, `memory`, `environment`). The builder emits the documented
   API-shaped request body; the CLI flag spelling is confirmed from
   `aws bedrock-agentcore-control create-harness help` at deploy time rather
   than guessed here.

## Testing

`tests/test_agentcore_harness.py`, hermetic (loopback uvicorn, no AWS, no
credentials, no internet). It asserts, against the **real** M4 MCP server's
advertised tool list over the wire:

- `allowedTools` equals the real read-only tool set (real tools minus
  `decide_ticket`), and MCP remains at nine tools;
- `decide_ticket` and any execution/mutation tool name are absent from the
  allowlist;
- exactly one `remote_mcp` tool is declared, no built-in or other AgentCore tool;
- memory is disabled and every limit is explicit and bounded;
- the harness name, HTTPS URL, role, and model inputs are validated;
- the harness prompt carries the same boundary rules as the local prompt (a
  shared-marker drift check), and the module imports no AWS SDK and contains no
  mutating call.

`tests/test_mcp_endpoint_auth.py`, hermetic (loopback uvicorn, no AWS, no
internet, Secrets Manager never called). It asserts:

- an unconfigured endpoint requires no credential and `gate()` is a true no-op,
  while a configured-but-unusable secret aborts startup (fail closed);
- the credential is read from the secret's `api_key` JSON field and its `repr`
  never contains the key;
- the ASGI gate refuses a missing, wrong, or prefix value with `401` before the
  app is reached, accepts the configured value (case-insensitive name, any
  occurrence), passes non-HTTP scopes through, and echoes nothing from the
  request;
- the **real** M4 server, wired through the environment variable, refuses the
  JSON-RPC initialize handshake anonymously and completes it when the header is
  present (session id returned);
- the deployed build (`host="0.0.0.0"`) accepts a load balancer `Host` while the
  loopback build answers `421`, pinning the measured transport behaviour;
- hygiene: module-level imports exclude boto3, boto3 is imported only inside the
  reader, `hmac.compare_digest` is used, and importing the module leaves boto3
  unimported.

The full suite is green (`1645 passed`) and `ruff check .` is clean.

## Deployed realization (M17, 2026-10-16)

The first authorized deployment of the design above. Account `527557823928`,
region `us-east-1`. Every resource was created by the scoped deploy role
`sws-m17-deployer` (assumed from the `sws-operator` SSO role); root cannot assume
roles in this account, so operator -> deploy-role is the only deployment path.

### Resources

| Piece | Identifier |
| --- | --- |
| API key secret | `sws-m17-mcp-api-key-7VwiAx` (JSON `{"api_key": ...}`; value never printed) |
| Certificate | `certificate/f8c3dc10-...` (ISSUED for `mcp.sjsujalgupta.xyz`) |
| Private subnets / RT | `subnet-0a5357916ffadf735`, `subnet-08451cf9ef5326b82`; RT `rtb-06b6787ffe8eda6f8` = `local` + S3 gateway only (**no NAT, no `0.0.0.0/0`**) |
| Security groups | `sws-m17-vpce-sg`, `sws-m17-task-sg`, `sws-m17-nlb-sg`, `sws-m17-harness-sg` |
| VPC endpoints (created) | `ecr.dkr`, `ecr.api`, `logs`, `secretsmanager`, `bedrock-runtime`, `s3` gateway |
| Image | `527557823928.dkr.ecr.us-east-1.amazonaws.com/sws-m17-mcp-server` |
| ECS | cluster `sws-m17-cluster`, service `sws-m17-mcp` (Fargate 1/1, `assignPublicIp=DISABLED`, target healthy) |
| NLB | `sws-m17-mcp-nlb` (internal), TLS 443 listener -> `sws-m17-mcp-tg` |
| Private DNS | hosted zone `Z03143173W3VFUCOJNOOD`; `mcp.sjsujalgupta.xyz` A -> NLB (private: not resolvable off the VPC) |
| Credential provider | `token-vault/default/apikeycredentialprovider/sws-m17-mcp-api-key` (EXTERNAL -> the secret, jsonKey `api_key`) |
| Harness | `sws_readonly_harness-WvQBBsqSoK` (READY) |
| Runtime | `harness_sws_readonly_harness-mAxtbhA3zn` (READY, VPC) |
| Roles | `sws-m17-deployer`, `sws-m17-ecs-execution-role`, `sws-m17-mcp-task-role`, `sws-m17-harness-role` |

### Verified at deploy

- The endpoint gate answers `401` without the header and completes the MCP
  handshake with it; the harness sends the credential-provider `${arn:...}`
  reference on `initialize` (§G.3), and the reference resolves (the remote tool
  loads rather than failing to start).
- `InvokeHarness` on `sws_readonly_harness-WvQBBsqSoK` completes a full turn: the
  model calls `sws_list_approvals` through the authenticated HTTPS MCP path and
  reports the tool's result.
- Harness config: `remote_mcp` only; `allowedTools` = the eight read-only tools in
  both `@sws/<name>` and bare form (16 entries; no `decide_ticket`, no
  execution/mutation tool); memory `disabled`; `sliding_window` 150;
  `maxIterations` 8; `timeoutSeconds` 120; model `amazon.nova-pro-v1:0`.
- Roles carry no mutation capability reachable from the model: the harness role
  has no `ec2:Stop*`/`Terminate*`, no `sts:AssumeRole`, no `iam:PassRole`; the
  task role is read-only (`s3:List*`, `lambda:List*`, `ec2:DescribeInstances`,
  `ce:GetCostAndUsage`, `sts:GetCallerIdentity`) plus reading its own secret.
- CloudTrail shows **no** `CreateGateway` and **no** `CreateMemory` events, and
  every `CreateHarness`/`CreateAgentRuntime`/`CreateServiceLinkedRole` event is
  attributable to the deploy role.

### Known limitation: VPC-mode AWS API egress (deferred)

In VPC mode the MCP task sits in private subnets whose route table has only the
`local` route and the S3 gateway endpoint. The MCP server builds **live**
`s3`/`lambda`/`ce`/`ec2` clients (`src/sws_agent/aws.py`), so tools that call AWS
APIs hang: `audit_workspace`/`collect_workspace` (Lambda) and `get_cost_estimates`
(Cost Explorer). With `connect_timeout=30` and five retries (`constants.py`), one
black-holed call blocks the server for minutes, surfacing to the harness as
`timeout_exceeded` (`Tool execution cancelled locally; remote execution may have
continued`). Tools that touch no AWS API -- `list_approvals`, and
`get_relationships`/`evaluate_workspace`/`explain_resource` over a caller-supplied
snapshot -- work end to end.

Making the inventory tools work in VPC mode requires giving the MCP task egress
to the AWS APIs it reads (interface VPC endpoints for `lambda`, `ce`, and `ec2`
for the optional `collect_ec2` pass, or a NAT gateway). Per the deploy decision
this is **out of scope for M17**; M17's boundary goal -- an authenticated HTTPS
MCP reachable from the harness exposing exactly the eight read-only tools -- is
met. Two transient interface endpoints (`bedrock-agentcore`,
`bedrock-agentcore-control`) were created while diagnosing the timeout and then
removed after confirming `InvokeHarness` succeeds without them.

## Local files touched by this ADR

- `src/sws_agent/agentcore_harness.py` -- config builder (pre-existing M17 work).
- `src/sws_agent/mcp/endpoint_auth.py` -- **new**: the opt-in caller-authentication
  gate.
- `src/sws_agent/mcp/server.py` -- `streamable_http_app()` forwards `host` and
  applies the gate; `run()` serves that same app (so the gate cannot be bypassed)
  with `uvicorn` directly instead of the SDK runner.
- `tests/test_agentcore_harness.py`, `tests/test_mcp_endpoint_auth.py` -- hermetic
  tests.
- `README.md`, `docs/reuse-decisions.md` -- scope notes.

The design and hermetic sections of this ADR create no infrastructure. The
M17 deployment they describe is recorded above under "Deployed realization
(M17, 2026-10-16)"; nothing here is committed until reviewed.
