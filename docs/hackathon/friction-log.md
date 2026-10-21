# Friction log — SWS hackathon build

This log records one concrete friction point encountered while deploying the SWS
public demo, what the expectation was, how it was diagnosed, and the exact change
made. It is evidence, not narrative: every claim below is traceable to a commit or
a file in this repository.

- Scope: the public demo's conversational layer (Strands + Amazon Bedrock).
- Related: `docs/adr/0015-public-demo-conversational-llm.md`, ADR 0014
  (`docs/adr/0014-public-demo-website-hosting.md`), `render.yaml`,
  `deploy/web/Dockerfile`.
- Change under discussion: commit `31d4223` —
  `fix(deploy): use non-streaming Bedrock Converse; sanitize agent audit args`.

## 1. Bedrock streaming vs. the least-privilege IAM identity

### Expectation

ADR 0015 enables the real Strands + Amazon Bedrock provider on the public demo
under a **Bedrock-only** IAM identity. The stated design control is a bounded,
one-shot conversational answer with **no streaming** (`AGENT_MAX_TOKENS = 768`,
`retry_strategy=None`, "no streaming" listed among the cost ceiling controls). The
intent is that the runtime needs only the least-privilege `bedrock:InvokeModel`
permission.

### What actually happened

The Strands `BedrockModel` used by `_model_invoker`
(`src/sws_agent/simulator/agent.py`) did not force a specific call shape. Left at
its default, the provider can drive the Bedrock **`ConverseStream`** path, whose
authorization is the separate action `bedrock:InvokeModelWithResponseStream`. That
would not be satisfied by an identity that holds only `bedrock:InvokeModel` — the
exact least-privilege scope ADR 0015 describes. The mismatch is between a stated
"no streaming" design control and a runtime that was not pinned to the
non-streaming API.

### Diagnosis

The friction is a **configuration/permission** mismatch, not a model or network
error: the demo re-renders a single full answer anyway (there is no incremental UI
consumer of a token stream), so streaming buys nothing here while widening the
required permission beyond the intended single action. The fix is to force the
provider onto the non-streaming Bedrock `Converse` API so the runtime's required
permission stays within `bedrock:InvokeModel`.

### Change made (commit `31d4223`)

In `src/sws_agent/simulator/agent.py`, the `_model_invoker` constructs the Strands
`BedrockModel` with `streaming=False`:

```
model = BedrockModel(
    model_id=model,
    max_tokens=AGENT_MAX_TOKENS,
    region_name=region_name,
    streaming=False,
)
```

The commit message records the intent: "`streaming=False` forces Strands to call
the non-streaming Bedrock `Converse` API instead of `ConverseStream`. This keeps
the runtime to the least-privilege `bedrock:InvokeModel` permission (no
`InvokeModelWithResponseStream`) and matches the 'no streaming' design control;
the demo re-renders a single answer anyway."

The same commit also strips model-invented cost arguments for collection tools in
`src/sws_agent/simulator/service.py` (keys `collect_cost`, `cost_end_date`,
`cost_window_days`, `cost_group_by` are removed from resolved tool args). Those
arguments never belonged in a deterministic demo audit — a model-invented,
non-ISO cost window could fail the tool — so removing them keeps the demo audit
deterministic. This is a separate concern bundled in the same commit.

### Verification

- `tests/test_simulator_provider.py::test_model_invoker_configures_bedrock_with_bounds`
  now asserts `captured["bedrock"]["streaming"] is False`, alongside the existing
  Nova Lite model id, token ceiling, region, no-tools/no-retry assertions added by
  this commit.
- The permission framing is consistent with the ADR record: ADR 0015 states the
  Bedrock-only identity's only permission is `bedrock:InvokeModel`/`Converse` on
  the chosen model, and states "no streaming" among the code-enforced cost
  ceilings.
- This is a runtime call-shape and permission-scope fix. It changes no UI, no
  workspace behavior, and no AWS resource.

### What this does **not** claim

- It does not claim `ConverseStream` was universally unavailable or broken. The
  friction is specifically that the least-privilege identity was scoped to
  `bedrock:InvokeModel`, and the runtime was pinned to match that scope rather than
  requiring a streaming permission the identity was not granted.
- It does not claim streaming is permanently impossible on Bedrock. The demo
  chooses the non-streaming API deliberately because it re-renders one answer.
- It does not claim the demo can mutate AWS or read AWS resources. The
  conversational layer makes a Bedrock inference call only; the SWS workspace
  remains synthetic (see `docs/hackathon/aws-services.md`).
