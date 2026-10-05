# ADR 0006: Dispatch evidence and bounded settle for `ec2:StopInstances`

- **Status**: Accepted (M15-C classification; M15-D handler and settle loop; IAM and the first live stop still out of scope)
- **Date**: 2026
- **Scope**: what a live `ec2:StopInstances` call would mean, how the system would know whether it reached AWS, how an asynchronous stop is settled, and what must be true before the first one is allowed to happen
- **Partially resolved**: facts 6 and 9 identified a gap in the *ledger's* contract. That gap is closed by [ADR 0007](0007-semantic-execution-re-execution-classification.md), which is implemented (schema version 3). The handler-side findings — the dispatch classifications, the settle loop, the pinning — were implemented in M15-D by `sws_agent/ec2_mutation.py` and `ExecutionCoordinator._settle`; see [ADR 0008](0008-stop-instances-handler-and-settlement.md). What is still open is everything that requires touching AWS or IAM. Line references in this document point at the ledger as it stood at M15-A; the decision they support is now implemented in ADR 0007.

## Context

ADR 0005 left SWS with a composition root that is structurally incapable of mutating: no handler that can reach an environment is registered anywhere, `STOP_RESOURCE.implemented` is `False`, and MCP exposes exactly nine tools. The first live write would be `ec2:StopInstances`.

Before writing that handler, the call was investigated. Ten facts constrain the design. Facts 1–5 were verified offline against the botocore service model shipped in this environment (`botocore 1.43.101`, `data/ec2/2016-11-15`); no AWS call was made. Facts 6–10 were read out of the current source.

**1. The operation is asynchronous, and there is no fast path.** The EC2 instance-state enum is `pending`, `running`, `shutting-down`, `terminated`, `stopping`, `stopped`. No state means "accepted and already stopped". A stop is accepted while the instance is `stopping` and reaches `stopped` later.

**2. `StopInstancesRequest.InstanceIds` is a list.** Multi-target is the API's native shape, not an abuse of it. `i-0123456789abcdef0` and `i-0123456789abcdef1` differ by one character, and both are valid arguments to a single call.

**3. `StopInstances` declares no modeled errors.** The operation's error list in the service model is empty. botocore raises `botocore.exceptions.ClientError` carrying `response["Error"]["Code"]`, and there is no typed exception to catch. Any classification must key on that structured code, the HTTP status, and the botocore exception class — and must not assume an exception type exists that carries the meaning.

**4. `StopInstancesRequest` has no `ClientToken`.** The operation cannot be made client-idempotent. Nothing the caller sends lets AWS recognise a repeat of the same logical request. Combined with fact 10, this is the irreducible ambiguity at the centre of this ADR: a retried or replayed stop is indistinguishable from a first stop, and no client-side setting can remove that.

**5. `StopInstances` supports `DryRun`.** The request can be authorized and validated without dispatching.

**6. The current post-attempt path misclassifies a healthy stop as a re-executable failure.** `ExecutionCoordinator._post_attempt_observe` observes once, immediately. `canonical_postconditions(STOP_RESOURCE)` is `{"state": "stopped"}`. `DefaultOutcomeVerifier` treats a missing expected fact as a direct contradiction. So an instance that is `stopping` — the healthy, expected state — yields `FAILED`, and `execution_ledger.py:1152` permits re-execution of any intent whose prior row is `resolved` with outcome `failed`. A stop that is working is recorded as a failure that may be attempted again.

**7. `MutationAttempt` cannot express what happened.** Its three fields (`ambiguous`, `call_error`, `sanitized`) collapse "the request was rejected before a byte left the process" and "EC2 accepted the stop and it is still stopping" and "the response was lost in flight" into nearly the same shape. The first is safely retryable, the second is a success in progress, the third is permanently ambiguous.

**8. `NOT_EXECUTED` exists but the coordinator cannot reach it.** `constants.py:515` defines `REFUSED` and `NOT_EXECUTED` as "the honest outcomes for requests that never crossed the mutation boundary". The post-handler path sets an outcome from the handler's own flags and then overwrites it with `_outcome_for_status(verification_status)` (`execution.py:531`, `execution.py:610`). A handler that never dispatched is still sent down the verification path and has its outcome derived from a verification it never earned.

**9. Only `failed` permits re-execution.** `execution_ledger.py:1152` selects a blocking prior with `state = 'resolved' AND outcome = 'failed'`. `RESOLVED` with `not_executed` is terminal *and* non-retryable, so a pre-dispatch failure — an expired role session, a wrong region, a missing tag — would permanently burn the intent with no legitimate retry path. This is a gap in the ledger's contract, not only in the handler's.

**10. The EC2 client inherits read-side retry policy.** `botocore.retries.standard` retries `RequestTimeout`, `RequestTimeoutException`, `PriorRequestNotComplete`, HTTP 500/502/503/504, `ConnectionError` and `HTTPClientError`, and throttling codes including `EC2ThrottledException`. Every one of those, occurring mid-mutation, is ambiguous. The current client is built once for observation and inventory; a mutation that inherits it may silently dispatch more than once while reporting one crossing.

## Decision

> **One `execute()` performs at most one HTTP dispatch of `StopInstances`. The dispatch disposition is recorded before verification is allowed to interpret it. An action whose post-state is asynchronous is settled by bounded polling, in which only `stopped` is success and every failure that cannot be classified with certainty is treated as ambiguous rather than clean.**

### One crossing, one dispatch

The mutation client is a separate EC2 client whose retry configuration disables SDK retries (`total_max_attempts: 1`). The observation client keeps standard retries, because a repeated `DescribeInstances` is harmless and its ambiguity costs nothing.

This makes "at most one dispatch per `execute()`" a property of the client configuration rather than a hope about network conditions, and it restores the property the single-crossing design has always rested on. Concretely: the handler issues exactly one call, so `MutationAttempt`'s disposition and the number of bytes that reached AWS are the same fact.

This does not remove ambiguity. By fact 4 no client can. `RequestTimeout` from EC2 means the service may or may not have queued the stop. The decision is to make that ambiguity **explicit and bounded** rather than to hide it inside a retry loop that reports one crossing.

### Dispatch disposition

`MutationAttempt`'s three booleans are replaced by one enumerated disposition plus evidence. Four values, because the safety-relevant distinction is not success versus failure but *what we know about the crossing*:

| Disposition | Meaning | Source of the classification |
| --- | --- | --- |
| `NOT_DISPATCHED` | No request left the process. | botocore raised before the request was written: parameter validation, credential, region, endpoint resolution, TLS/proxy handshake. |
| `DISPATCH_REJECTED` | A request was sent and the service refused it. Nothing was applied. | `ClientError` with a 4xx status and a classified code. |
| `ACCEPTED` | EC2 returned 200 with a `StopInstancesResult`. The stop is in progress. | `response["StoppingInstances"]`, cross-checked against `CurrentState`. |
| `DISPATCH_UNKNOWN` | A request may or may not have reached and been applied by EC2. | Any transport failure after the request was written, any 5xx, any unrecognised error code. |

The default is `DISPATCH_UNKNOWN`. An error code not in the classification table is not "probably fine" — it is ambiguous, and ambiguity is the safe direction because `UNKNOWN` blocks re-execution rather than permitting it.

**As built in M15-C, the disposition is stronger than "defaults to `DISPATCH_UNKNOWN`".** `DispatchEvidence.disposition` has *no* default and `extra="forbid"` applies, so there is no object that means "a crossing happened, trust me". A handler that returns nothing, raises, or returns a stale shape fails closed to `UNKNOWN` rather than being coerced into a value. This is the same requirement ADR 0005 and ADR 0006 impose elsewhere, applied to the boundary itself.

`NOT_DISPATCHED` must short-circuit verification entirely. The coordinator may not derive an outcome for an attempt whose boundary was never crossed; per fact 8 that is exactly the bug. The mapping is `NOT_DISPATCHED → NOT_EXECUTED`, `DISPATCH_REJECTED → FAILED`, `ACCEPTED → settle`, `DISPATCH_UNKNOWN → UNKNOWN`.

The mapping `NOT_DISPATCHED → NOT_EXECUTED` exposed fact 9 as a required ledger change: `not_executed` must become retryable, or be deliberately refused with a human path. Recording a pre-dispatch failure as permanently terminal would be a new safety bug in the name of honesty. **This was the largest contract change this ADR required. It is now designed and implemented:** ADR 0007 settled the vocabulary, M15-B implemented `ReexecutionClass` in the ledger, and M15-C made `NOT_DISPATCHED` the only route to a retryable `NO_EFFECT`.

### Settle: bounded polling, and only `stopped` is success

The canonical postcondition is unchanged: `{"state": "stopped"}`. Success is still derived from the action and never from the request (ADR 0001 rule 4). What changes is that a state which is *not yet* the expected one is no longer classified as a *contradiction* of it.

That requires a per-state disposition where the current verifier has only confirm-or-contradict. The disposition is action-derived and lives beside the postcondition in the canonical registry, so it cannot be supplied by a caller:

| Observed `state` | Disposition | Settle behaviour |
| --- | --- | --- |
| `stopped` | `CONFIRMED` | Success. Stop polling. |
| `stopping` | `PENDING` | Progressing. Keep polling. |
| `running` | `PENDING` | EC2 propagates state asynchronously; `DescribeInstances` can still report `running` immediately after acceptance. Keep polling; contradicted at the deadline. |
| `shutting-down` | `CONTRADICTED_TERMINAL` | `stopped` is unreachable. Stop polling. Failed, never re-executable. |
| `terminated` | `CONTRADICTED_TERMINAL` | The resource is gone. Stop polling. Failed, never re-executable. |
| `pending` | `CONTRADICTED_TERMINAL` | EC2 rejects a stop on a `pending` instance with `InvalidInstanceState`. Stop polling. Failed, never re-executable. |
| instance absent | `CONTRADICTED_TERMINAL` | `InvalidInstanceID.NotFound` is a terminal fact about the target, not an observation error. Failed, never re-executable. |

`pending` and `terminated` are terminal here for the same reason botocore's own `InstanceStopped` waiter marks them failure acceptors. `shutting-down` is terminal for a stronger reason: an instance being terminated can never reach `stopped`, so continuing to poll would burn the entire deadline to learn nothing.

**`CONTRADICTED_TERMINAL` must be non-re-executable, and today it cannot be.** All four terminal rows above would be recorded `FAILED`, which `execution_ledger.py:1152` permits re-executing forever. Re-running `StopInstances` against a terminated instance does not stop anything; it produces `InvalidInstanceID.NotFound` forever. This is the same missing distinction as fact 9, and one change — a non-retryable failure — resolves both.

Settle parameters, and why each:

| Parameter | Value | Basis |
| --- | --- | --- |
| Poll interval | 15 s | botocore's `InstanceStopped` waiter uses `delay: 15`. |
| Deadline | 600 s | The same waiter uses `maxAttempts: 40` — 15 × 40 = 600 s. The bound is adopted by reference to AWS's own client-side precedent rather than invented here. |
| Backoff | none | This polls a state, not a throttled endpoint. A fixed interval is simpler to reason about and to reconstruct from evidence. |
| Consecutive observation errors | 3 | An unavailable observation is not a contradiction. Three consecutive failures end the settle as `UNKNOWN` rather than spending ten minutes on a broken network. |
| Evidence | one audit record per poll | Each record carries attempt id, instance id, account, region, observed state, observed timestamp. At most 40 records per attempt; bounded. |

At the deadline: `CONFIRMED` → success; `PENDING` → `UNKNOWN`, therefore `UNRESOLVED`, therefore not re-executable without the runbook in `docs/runbooks/first-live-stop-instances.md`; `CONTRADICTED_TERMINAL` → `FAILED`, non-re-executable; `running` → `FAILED`, re-executable, because there is no evidence the stop took any effect at all and a retry is the correct remedy.

`UNKNOWN` on deadline expiry is the load-bearing choice. The intent "this instance is stopped" was not confirmed, so it is not reported as success — but AWS told us it is `stopping`, so calling that a *failure* would assert something the evidence contradicts. `UNRESOLVED` is the honest state: the request was dispatched and accepted, convergence was not observed.

The first observation is not a poll. The `StopInstancesResult` carries `PreviousState` and `CurrentState` as first-party evidence of what EC2 did, and that pair is recorded before any `DescribeInstances` call. It is the only direct statement of the service's intent available, and it is what makes a `running` first observation interpretable rather than alarming.

### Error classification

Every key below is structured — `ClientError.response["Error"]["Code"]`, `ClientError.response["ResponseMetadata"]["HTTPStatusCode"]`, or the botocore exception class. No message text is parsed. Per fact 3 there is no typed service exception to catch, so `Error.Code` equality against an explicit allow-list is the only structured discrimination available.

The cost of the allow-list was revised at M15-C sign-off. This section originally held that "an unrecognised code must fall to `DISPATCH_UNKNOWN`". That is wrong, and the reason is that **refusal and comprehension are separate facts**. A 4xx from EC2 establishes that nothing was applied regardless of whether the code is one we recognise; only the system's *understanding of why* is missing. Falling to `DISPATCH_UNKNOWN` would have discarded an established absence of effect and turned an ordinary throttle into a terminal unknown, so the ruling is instead:

| Signal | Disposition | Re-execution class |
| --- | --- | --- |
| 4xx whose code is on the action's `rejection_classes` list | `DISPATCH_REJECTED` | as declared for that code |
| 4xx whose code is **not** on that list | `DISPATCH_REJECTED` | `TRANSIENT_REJECTION` — refusal is established, so the intent may be retried under a fresh authorisation |
| 5xx, or a `ClientError` with no readable response | `DISPATCH_UNKNOWN` | `OUTCOME_UNKNOWN` — the service may have applied the change before failing |

`DISPATCH_UNKNOWN` is therefore reserved for the genuinely unestablished case, which is transport ambiguity rather than an unrecognised code.

| Structured signal | Disposition | Re-executable |
| --- | --- | --- |
| `ParamValidationError` | `NOT_DISPATCHED` | yes |
| `NoCredentialsError`, `PartialCredentialsError`, `CredentialRetrievalError` | `NOT_DISPATCHED` | yes |
| `NoRegionError`, `UnknownRegionError`, `InvalidRegionError` | `NOT_DISPATCHED` | yes |
| `EndpointResolutionError`, `UnknownEndpointError`, `BaseEndpointResolverError` | `NOT_DISPATCHED` | yes |
| `SSLError`, `ProxyConnectionError`, `InvalidProxiesConfigError` | `NOT_DISPATCHED` | yes — the TLS or proxy handshake precedes the request |
| `ClientError` `DryRunOperation` (403) | `NOT_DISPATCHED` | yes — `DryRun: true` applies nothing, by design |
| `ClientError` 4xx: `UnauthorizedOperation`, `AccessDenied`, `AccessDeniedException`, `AuthFailure`, `InvalidParameterValue`, `InvalidParameterCombination`, `MissingParameter`, `RequestExpired`, `RequestTimeTooSkewed`, `InvalidInstanceID.Malformed` | `DISPATCH_REJECTED` | yes — the rejection is established |
| `ClientError` `RequestLimitExceeded`, `Throttling`, `EC2ThrottledException` | `DISPATCH_REJECTED` | yes — the operator re-runs through the gate |
| `ClientError` `InvalidInstanceID.NotFound` | `DISPATCH_REJECTED` | **no** — terminal fact about the target |
| `ClientError` `InvalidInstanceState` | `DISPATCH_REJECTED` | **no** — the state forbids a stop |
| `ClientError` 5xx: `InternalError`, `InternalServerError`, `ServiceUnavailable` | `DISPATCH_UNKNOWN` | **no** |
| `EndpointConnectionError`, `ConnectTimeoutError` | `DISPATCH_UNKNOWN` | **no** — see below |
| `ReadTimeoutError`, `ConnectionClosedError`, `IncompleteReadError`, `ResponseStreamingError`, `ConnectionError`, `HTTPClientError` | `DISPATCH_UNKNOWN` | **no** |
| any other code, any unrecognised exception | `DISPATCH_UNKNOWN` | **no** |

Throttling appears in the table as a service response rather than an SDK retry for the same reason fact 10 applies: whether the request was queued is not something the client can know, and a human deciding to re-run through the gate is a better answer than a silent loop.

### Resolution (M15-D prerequisite): `EndpointConnectionError` is `DISPATCH_UNKNOWN`

**This reverses an earlier decision in this ADR.** The original table classified
`EndpointConnectionError` and `ConnectTimeoutError` as `NOT_DISPATCHED` — re-executable
— on the argument that DNS resolution and the TCP handshake both failed, so no HTTP
request was written to the socket. That argument is **withdrawn**.

A connection failure during a mutation has two possible histories that the exception
cannot distinguish:

```text
client ──▶ network failure
          request never reaches EC2
          └─▶ EndpointConnectionError

client ──▶ EC2
          request transmitted and queued
          response lost on the way back
          └─▶ EndpointConnectionError
```

In the second case the stop was applied. The single exception carries both, so
classifying it `NOT_DISPATCHED` would manufacture evidence that the mutation did not
happen — the exact defect M15-C exists to eliminate. It was also the single asymmetry
in the design: `DISPATCH_REJECTED` was treated as established because AWS answered,
while this transport failure was treated as *more* informative than it actually is.

`botocore` exposes no signal that separates the two cases. Distinguishing them would
require transport-level observation the client does not perform, so the honest
classification is `DISPATCH_UNKNOWN`, which settles as `UNKNOWN` / `OUTCOME_UNKNOWN` /
`UNRESOLVED` and permits no automatic re-execution.

**What this costs.** A connect failure before a mutation is now terminal rather than
cheaply retryable, and connect failures against a healthy endpoint are not rare. The
price is real and is accepted deliberately: an operator re-running through the gate
costs one human step, while a falsely-recorded `NO_EFFECT` costs correctness the system
cannot detect on its own. The asymmetry is the point — the conservative direction is
the one that spends a human's time.

`NoCredentialsError`, `NoRegionError`, and `ParamValidationError` keep their
`NOT_DISPATCHED` classification, and unlike a connect failure these *are* established:
no request could have been written because the client could not have produced a valid
one. The test applied throughout this table is whether the absence of effect is
**established**, not whether the error is "early" in some informal sense.

### IAM: a separate role, scoped to one instance

The mutating capability gets its own credential and is never added to the read-only collection principal.

The role's policy is exactly two actions:

- `ec2:StopInstances` scoped by `Resource` to the single sacrificial instance ARN, `arn:aws:ec2:<region>:<account>:instance/<id>`. A mutating action scoped to one resource is supported by EC2's resource-level permissions and is the strongest control available here.
- `ec2:DescribeInstances` on `Resource: "*"`, because `Describe*` does not support resource-level scoping. The wildcard is unavoidable for the read; it is acceptable because the action cannot mutate. This is stated rather than hidden.

`StopInstances` is also constrained by `aws:RequestedRegion` and, as defence in depth beyond the ARN, by an `ec2:ResourceTag` condition requiring the sacrificial ownership tag. The ARN already pins the target; the tag condition means a wrong instance id alone still fails.

The trust policy names the private CLI's role explicitly, with an `ExternalId` where the chain is cross-account. No root principal, no wildcard principal.

Credentials are supplied explicitly to the mutation client. There is no fallback to the environment, a shared profile, or an instance-profile role: if the intended role cannot be assumed, the result is `NOT_DISPATCHED` and the run stops. A credential fallback is how "the sacrificial instance" becomes "whatever this environment could reach".

### Target pinning

Per fact 2, multi-target is prevented actively rather than by convention:

- The request asserts `len(InstanceIds) == 1` before the call. This is a refusal, not an assertion that is logged.
- The target id is derived from an independently observed identity and compared to the approved id. `ExecutionRequest.resource_id` is caller-supplied and is never the source of the id sent to AWS.
- Account and region are pinned and re-derived from the session, not from configuration that could drift.
- The state and ownership tag are re-observed immediately before dispatch, closing the window between approval and effect.
- `Force`, `Hibernate`, and `SkipOsShutdown` are set explicitly to `false` rather than left to API defaults. Each changes what the call means: `Force` bypasses OS shutdown, `SkipOsShutdown` stops without a clean shutdown, and `Hibernate` turns a stop into a hibernate.
- `Hibernate` is not merely a semantic default to pin. The EC2 state enum available to `DescribeInstances` has no hibernated member, so a hibernated instance could never be observed as `stopped` and the settle loop could only ever time out. Pinning it `false` is a precondition for the settle loop being able to observe success at all.
- `DryRun: true` is issued with the same role and the same parameters immediately before the real call, as the final pre-dispatch gate. `DryRunOperation` (403) confirms the role is authorized and the parameters are valid without applying anything. It is a check, not a guarantee: the real call can still fail differently.

Irreversible wrong-target causes, each mapped to the control above:

| Cause | Control |
| --- | --- |
| `InstanceIds` with more than one element | length refusal |
| Same id in another region or account | account and region pinned and re-derived from the session |
| Credential resolved from the ambient chain | explicit role, no fallback |
| Target changed between approval and dispatch | re-observation of id, account, region, state, and tag immediately before dispatch |
| Request replayed after `FAILED` against a freshly sourced target | id pinned to the approved record, never re-queried at execution time |
| Defaults silently changing semantics | `Force`, `Hibernate`, `SkipOsShutdown` set explicitly |
| Operator error at a manual invocation | the CLI requires the pinned id to be echoed and refuses a mismatch |

### What this ADR does not do

It implements nothing. `STOP_RESOURCE.implemented` stays `False`; no handler that can mutate is registered; MCP stays at exactly nine tools with no path to a composition. No retry, IAM, runbook, or ledger behaviour described here exists in code.

No reconciliation is designed. `UNRESOLVED` stays terminal. No stale-reservation takeover is proposed.

### Status of the two contract changes this ADR exposed

The original text of this section said that neither the non-retryable failure distinction (fact 9) nor the disposition replacing `MutationAttempt`'s booleans (fact 7) was designed, and that implementing either without the other would reintroduce the original defect under a new name. That is no longer true, and the sentence has been removed rather than amended, because the reason it was true has changed:

- **Fact 7 (the disposition) is now designed and implemented.** ADR 0007 settled the classification vocabulary; M15-B implemented it in the ledger as `ReexecutionClass`; M15-C implemented the evidence type (`DispatchEvidence` with a required `DispatchDisposition`) and the coordinator-side classifier that consumes it. The booleans are gone. `MutationAttempt` no longer exists.
- **Fact 9 (the non-retryable failure distinction) is likewise settled**, in ADR 0007's six-class vocabulary, of which `TARGET_INVALID` and `POST_STATE_UNREACHABLE` are exactly the terminal states this ADR identified as unreachable.

The populated content arrived in M15-D, which is also where this ADR's settle loop was implemented. `STOP_RESOURCE` now declares a real `dispatch_contract` and a `SettlePolicy`, and `sws_agent/ec2_mutation.py` supplies the `StopInstances` call this ADR was waiting on.

What remains unimplemented is the part that requires touching AWS or IAM: the dedicated client factory with pinned credentials, the scoped `ec2:StopInstances` policy, and the live first stop. ADR 0008 carries that design. `STOP_RESOURCE.implemented` stays `False` until independent review of the handler, and no call has been made.

## Alternatives considered

**Poll until `stopped` inside `execute()` with no deadline.** Rejected. It turns a bounded operation into an unbounded one and makes a crash indistinguishable from a hang, with no durable evidence of how far it got.

**Report success on `stopping`.** Rejected. It redefines the success criterion to match what the API happens to return, which is the move ADR 0001 rule 4 and ADR 0005 both exist to prevent. `stopped` is the intent; `stopping` is a fact about progress toward it.

**Record deadline expiry as `FAILED`.** Rejected. It would permit re-execution on the strength of a state AWS reported as healthy, and it asserts a failure the evidence contradicts. `UNKNOWN` costs a human step; `FAILED` here would cost correctness.

**Keep botocore standard retries and record one crossing per `execute()`.** Rejected. The crossing count would be a claim about the call rather than about the wire, and by fact 4 nothing could check it.

**Use a client token to make the call idempotent.** Unavailable. `StopInstancesRequest` has no `ClientToken` (fact 4). This is the reason the ambiguity is designed around rather than engineered away.

**Handle ambiguity with a compensating stop or a follow-up start.** Rejected, and out of scope entirely. A second mutation cannot disambiguate the first; it only adds an effect to reconcile.

**Give the mutation role `ec2:*` on the account.** Rejected. The capability needed is one action on one instance, and a broader grant makes any future mistake in the handler a full-account event.

**Reuse the observation client and widen it.** Rejected. Fact 10: read-side retry policy is wrong for mutations, and one client serving both means every observation path inherits mutation ambiguity.

## Consequences

The design converts a live stop from a single ambiguous boolean into three recorded facts — how many dispatches were attempted, what the service said, and how the post-state converged — each independently auditable. The cost is that the first live execution holds a worker for up to 600 seconds and appends up to forty settle records, and the coordinator grows a branch it does not have today.

Three risks were carried forward at the time this ADR was written. Two are now closed
and one stands.

The largest — the missing non-retryable failure (fact 9), which left every
`CONTRADICTED_TERMINAL` and every `NOT_DISPATCHED` path either permanently blocked or
indefinitely re-executable — **is closed.** ADR 0007 settled the vocabulary, M15-B
implemented `ReexecutionClass` in the ledger, and M15-C made `NOT_DISPATCHED` the only
route to a retryable `NO_EFFECT`.

The second — the `EndpointConnectionError → NOT_DISPATCHED` classification — **is
also closed, in the opposite direction to the one proposed here.** The reasoning it
rested on (no TCP connection, therefore no bytes written) does not survive contact with
a lost response, and `botocore` offers no signal to separate the two cases. The
classification is now `DISPATCH_UNKNOWN`; see the resolution section above. The cost
this ADR called "an unnecessary human step on a common pre-flight failure" is now
accepted as the price of not manufacturing evidence that a mutation did not happen.

The third risk stands: `running` is `PENDING` for the full window. If EC2 ever declined
a stop without changing state, the loop would poll to the deadline and then report
`FAILED` and permit a retry, which is correct but slower than a decisive signal. This
is the accepted price of not treating a propagation lag as a contradiction.

Finally, the evidence this design records is evidence about SWS and about what one EC2 API returned. Nothing in it is evidence that stopping an instance is safe in general.
