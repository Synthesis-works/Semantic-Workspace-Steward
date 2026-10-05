# ADR 0008: the `StopInstances` handler, its settlement loop, and the mutation client

- **Status**: Accepted — implemented in M15-D, except the client factory and IAM, which
  remain open. `STOP_RESOURCE.implemented` is still `False` pending independent review.
- **Date**: 2026
- **Scope**: the first real mutation boundary for `STOP_RESOURCE`, the bounded
  settlement loop that follows it, the dedicated mutation client, and the
  `DispatchContract` that makes `ec2:StopInstances` evidence classifiable. Does
  not provision anything, call AWS, or change the MCP surface.

## What was implemented

- `sws_agent/ec2_mutation.py` — `StopInstancesHandler`. Dispatches once, pins
  `Force`/`Hibernate`/`SkipOsShutdown`, refuses a client that could retry, and maps
  each failure to exactly one `DispatchDisposition`. Never imports `boto3` or
  `botocore`; the client is injected.
- `ExecutionCoordinator._settle` and `settle_policy_for` — the coordinator-owned
  bounded wait, with `SettlePolicy` injected and the clock and sleep seams
  injectable. The handler never waits.
- `STOP_RESOURCE`'s `SettlePolicy` and populated `DispatchContract`.
- `tests/test_m15d_stop_instances.py` — 57 tests, including the one-dispatch /
  many-observations invariant and the exhaustive `NO_EFFECT` check.

Still open: the client factory that pins credentials and disables SDK retries, the
scoped `ec2:StopInstances` policy, and the first live stop. Nothing in this ADR has
contacted AWS.

## Context

ADR 0006 designed the `StopInstances` mutation and left two contract changes
unsettled. Both are now settled, and this milestone builds on them:

- ADR 0007 and M15-B gave the ledger a `ReexecutionClass` vocabulary with a
  compatibility table the ledger enforces.
- M15-C replaced `MutationAttempt`'s two booleans with `DispatchEvidence` and a
  required `DispatchDisposition`, made `classify_dispatch` the only function in
  `src/` that produces a `ReexecutionClass`, and established the negative
  guarantee that **`NO_EFFECT` is reachable from `NOT_DISPATCHED` and nowhere
  else**.

What does not exist is anything that produces `ACCEPTED` or `DISPATCH_REJECTED`
for a real API call, and nothing that waits for an asynchronous post-state to
converge. `STOP_RESOURCE.implemented` is `False` and the only shipped handler
reports `NOT_DISPATCHED`.

Three properties of the target make this harder than a synchronous call.

**The target state is asynchronous.** `StopInstances` returns 200 while the
instance is `stopping`. Success is `{"state": "stopped"}`, so the request
completing and the intent succeeding are different events, separated by
seconds or minutes. A single observation immediately after dispatch observes
`stopping` — the healthy expected state — and would record it as a
postcondition contradiction. ADR 0006 fact 6.

**The call is not idempotent and cannot be made so.**
`StopInstancesRequest` has no `ClientToken`, so SWS cannot ask AWS to collapse a
repeated stop into one. Retrying is therefore a decision about *what is known*,
never a safe default.

**Transport failures do not reveal whether the call landed.** Resolved in ADR
0006 at M15-D sign-off: `EndpointConnectionError` and `ConnectTimeoutError` are
`DISPATCH_UNKNOWN`, not `NOT_DISPATCHED`, because a lost response and a
pre-send failure are the same exception.

## Decision

> **The handler dispatches once and reports facts. A coordinator-owned bounded
> settle loop waits for convergence. `classify_dispatch` turns the resulting
> evidence into a basis, and the ledger records it.** The handler never names an
> outcome or a retry policy, and nothing in this design provisions or contacts
> AWS.

### The boundary is one call, and the client cannot retry it

`StopInstancesHandler` holds a dedicated EC2 client configured
`retries={"total_max_attempts": 1}`. This is the single most important
mechanical property of the design: botocore's standard mode otherwise retries
`RequestTimeout`, 5xx, and throttling codes, meaning one `handle()` call could
put several requests on the wire while SWS recorded one crossing. The number of
bytes that reached AWS and the number of dispatches SWS recorded would stop
being the same fact.

The client is separate from the read-side client used by collection and
observation. That client keeps standard retries, because a repeated
`DescribeInstances` is harmless and its ambiguity costs nothing; the same policy
applied to a mutation is not harmless at all.

Credentials are supplied explicitly to the mutation client. There is no
fallback to the environment, a shared profile, or an instance-profile role. A
credential fallback is how "the sacrificial instance" becomes "whatever this
environment happened to be able to reach". If the intended role cannot be
assumed, the result is `NOT_DISPATCHED` and the run stops.

### The request is pinned, not parameterised

Exactly one `InstanceIds` element, from the already-gated `resource_id`. A
multi-instance stop would make a partial `StoppingInstances` response
uninterpretable — one instance stopping says nothing about the others — and
would put several resources behind one intent key.

`Force=False`, `Hibernate=False`, `SkipOsShutdown=False` are sent explicitly
rather than defaulted. Each changes the meaning of the call rather than merely
its speed: `Force` stops an instance that has pending instance-store tasks or
is in a transitional state that `Hibernate` would preserve. Defaults would let a
future botocore release change what "stop" means without any SWS change.

### Evidence mapping

The handler converts botocore outcomes into `DispatchEvidence` using ADR 0006's
table as amended at M15-D sign-off:

| botocore outcome | disposition |
|---|---|
| `stop_instances` returns a `StoppingInstances` result | `ACCEPTED` |
| `ParamValidationError`, `NoCredentialsError`, `PartialCredentialsError`, `CredentialRetrievalError` | `NOT_DISPATCHED` |
| `NoRegionError`, `UnknownRegionError`, `InvalidRegionError` | `NOT_DISPATCHED` |
| `EndpointResolutionError`, `UnknownEndpointError`, `BaseEndpointResolverError` | `NOT_DISPATCHED` |
| `SSLError`, `ProxyConnectionError`, `InvalidProxiesConfigError` | `NOT_DISPATCHED` |
| `ClientError` with `DryRunOperation` | `NOT_DISPATCHED` |
| `ClientError`, HTTP 4xx | `DISPATCH_REJECTED`, carrying `Error.Code` |
| `ClientError`, HTTP 5xx | `DISPATCH_UNKNOWN` |
| `EndpointConnectionError`, `ConnectTimeoutError` | `DISPATCH_UNKNOWN` |
| any other exception | `DISPATCH_UNKNOWN` |

Every entry is a structured signal — exception class, HTTP status, `Error.Code`.
No message text is parsed. `Error.Code` equality against an explicit allowlist
is the only structured discrimination available, because `StopInstances` models
no typed exception (ADR 0006 fact 3).

The 4xx/5xx split is the load-bearing line. A 4xx means AWS received the
request, understood it, and refused it — so nothing was applied and
`DISPATCH_REJECTED` is an established fact. A 5xx means the service may have
acted before failing, so nothing is established and the disposition is
`DISPATCH_UNKNOWN`.

The handler attaches no basis. It cannot: `DispatchEvidence` has no
`reexecution_class` field and `extra="forbid"` rejects one. It returns
`aws_error_code` and lets `classify_dispatch` decide what that code means,
which is the only way the "handler reports facts, classifier decides policy"
separation survives contact with a real error code.

### The settle loop belongs to the coordinator, not the handler

`handle()` dispatches once and returns `ACCEPTED`. It does **not** poll.

This is the design decision most likely to be questioned, so it is worth being
explicit about the alternative. Putting the loop inside the handler would mean a
600-second `handle()` call. A handler is the seam SWS holds to a single
statement about one crossing; making it also the place that waits makes that
statement hard to audit, because the return value now describes a fifteen-minute
process rather than a dispatch. It also gives the handler a second job —
observation — which is the job `ObservationProvider` already has, and duplicating
it would mean two places that read instance state.

Instead, `ActionSpec` gains an optional `SettlePolicy`, and on `ACCEPTED` the
coordinator drives the loop through the injected `ObservationProvider` it already
uses for the A5 gate. The handler stays a boundary adapter; settlement is
coordinator policy; observation has exactly one owner.

`SettlePolicy` for `STOP_RESOURCE`:

| parameter | value |
|---|---|
| `poll_interval_seconds` | 15 |
| `deadline_seconds` | 600 (40 polls) |
| `settled_states` | `stopped` |
| `in_progress_states` | `stopping`, `running` |
| `max_consecutive_observation_errors` | 3 |

`stopped` is the only success. `stopping` and `running` are both *progress*:
`stopping` is the expected healthy state and `running` means the stop has not
taken hold yet, and neither is evidence that the stop failed. A postcondition
contradiction — the verifier returning `FAILED` — is **not** something the loop
resolves by retrying; it exits to `classify_dispatch`, which decides the basis
from the observed state.

Deadline expiry reports `POST_STATE_NOT_REACHED` via the observed state rather
than inventing a failure. The instance is `running`, which is a state in which
repeating the stop is safe. Recording expiry as a terminal failure would be
slower but not wrong; recording it as *success* would be fabrication.

### The dispatch contract makes `StopInstances` evidence classifiable

Without this, every rejection and every contradiction fails closed to `UNKNOWN`
— correct, but it would mean the first live stop could only ever end in
`UNRESOLVED`. The contract is what lets the loop's states mean something:

```python
DispatchContract(
    rejection_classes={
        "InvalidInstanceID.NotFound": TARGET_INVALID,
        "InvalidInstanceID.Malformed": TARGET_INVALID,
        "InvalidInstanceState": POST_STATE_UNREACHABLE,
        "UnauthorizedOperation": TRANSIENT_REJECTION,
        "AccessDenied": TRANSIENT_REJECTION,
        "AuthFailure": TRANSIENT_REJECTION,
        "RequestLimitExceeded": TRANSIENT_REJECTION,
        "RequestExpired": TRANSIENT_REJECTION,
    },
    post_state_classes={
        "running": POST_STATE_NOT_REACHED,
        "stopping": POST_STATE_NOT_REACHED,
        "terminated": POST_STATE_UNREACHABLE,
        "shutting-down": POST_STATE_UNREACHABLE,
    },
)
```

Two entries carry the reasoning.

`InvalidInstanceState` maps to `POST_STATE_UNREACHABLE`, not
`TARGET_INVALID`. They are both terminal, but the distinction matters for a
future reader: the instance exists and the intent is well-formed, the current
state simply forbids it. `TARGET_INVALID` is reserved for identifiers that name
nothing.

`pending` appears in **neither** post-state map. An instance in `pending` is
mid-transition and cannot be stopped, but it also is not yet in a state that
proves the stop is impossible. Leaving it unclassified is deliberate: the
classifier treats an unmapped state as fail-closed, and a human resolving a
`pending` instance is cheaper than SWS asserting the target is unreachable on
the strength of a state that is about to change.

### IAM

`StopInstances` scoped by `Resource` to the single sacrificial instance ARN, plus
`ec2:DescribeInstances` on `Resource: "*"` because `Describe*` does not support
resource-level scoping. Constrained by `aws:RequestedRegion` and, as defence in
depth, by an `ec2:ResourceTag` condition requiring the sacrificial ownership
tag: the ARN pins the target, and the tag means a wrong instance id alone still
fails. The read wildcard is unavoidable and is stated rather than hidden —
`DescribeInstances` cannot mutate.

The trust policy names the private CLI's role explicitly, with an `ExternalId`
where the chain is cross-account. No root principal, no wildcard principal.

## What this ADR deliberately does not do

**`STOP_RESOURCE.implemented` stays `False`.** The handler will exist and be
testable, but flipping `implemented` requires independent review of the code, not
of this document.

**No AWS call.** Everything is verified against a fake client and a stubbed
`botocore` exception surface. The first live call is a separate milestone with
its own authorisation, gated on the sacrificial instance existing.

**No provisioning.** The sacrificial instance, its role, its policy, and its
tags are not created here.

**MCP stays at exactly nine tools**, and no MCP surface reaches a mutating
composition.

**No reconciliation.** `UNRESOLVED` remains terminal. A `DISPATCH_UNKNOWN` stop
that actually landed leaves a stopped instance and an unresolved row; that is the
correct record, and resolving it is deliberately a human decision rather than an
automatic takeover, because a stale-reservation takeover would have to guess
whether the original call applied.

## Consequences

A live stop becomes four independently auditable facts — the disposition, the
provider's code, the settlement trace, and the final basis — where M9 had one
ambiguous boolean. The cost is a worker held for up to 600 seconds and up to
forty settle records per execution, and a coordinator branch that does not exist
today.

The sharpest new failure mode is a settlement loop that cannot tell a
propagation lag from a dropped request. A `running` instance after 600 seconds is
reported as `POST_STATE_NOT_REACHED` and is retryable, which is right; an
instance whose state never changes because the request was silently dropped is
also `POST_STATE_NOT_REACHED`, which permits a retry that re-issues a stop AWS may
already have applied. `StopInstances` on an already-stopped instance is harmless,
so this is tolerable for *this* action and would not be for every action. That
limitation is a property of the target, not of the mechanism, and it is the reason
the basis vocabulary is per-action rather than global.

## Alternatives considered

**Put the settle loop inside the handler.** Rejected. It makes `handle()` a
fifteen-minute call whose return value describes a process rather than a
crossing, and gives the handler a second job that `ObservationProvider` already
owns. The cost of rejection is one new coordinator branch.

**Report success on `stopping`.** Rejected. It redefines success to match what the
API returns, which ADR 0001 rule 4 and ADR 0005 both exist to prevent. `stopped`
is the intent; `stopping` is a fact about progress toward it.

**Record deadline expiry as a terminal failure.** Rejected. It asserts a failure
the evidence contradicts and permanently consumes an intent whose target is still
in a retryable state.

**Classify `EndpointConnectionError` as `NOT_DISPATCHED`.** Rejected, reversing
the original ADR 0006 position. A lost response and a pre-send failure are the
same exception, and `botocore` exposes no signal to separate them, so the
classification would manufacture evidence that the mutation did not happen.

**Let the handler declare which error codes are terminal.** Rejected. It is the
exact inversion of M15-C: the layer that observes a string should not decide what
a string means for re-execution. The handler reports `aws_error_code`;
`DispatchContract` interprets it.

**Batch multiple instances per call.** Rejected. A partial `StoppingInstances`
response would be uninterpretable, and several resources would share one intent
key.

**Reuse the read-side client and widen its retry configuration per call.**
Rejected. Retry policy is a property of the client, not of a call site, and one
client serving both means every read path inherits mutation ambiguity or every
mutation inherits read-side leniency.
