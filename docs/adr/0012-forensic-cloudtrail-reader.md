# ADR 0012: The forensic CloudTrail reader

- Status: **accepted and ratified — closed for SWS v1.** This ADR records the v1
  decision that the reader is a standalone forensic component and is deliberately
  **not** wired into the live mutation path. It authorizes no live stop.
- Milestone: M16 (first-live-stop forensic reader)
- Date: 2026-10-05
- Depends on: ADR 0009 (production client and scoped IAM), ADR 0010 (single-dispatch
  witness and preflight gate), ADR 0011 (CloudTrail as independent AWS-side
  verification)

## Context

ADR 0011 specified CloudTrail verification as two halves and kept them apart on
purpose: a **reader** that retrieves AWS-side evidence, and a **judge** that rules
on it. M15-G implemented the judge. `sws_agent.reconciliation` consumes
`CloudTrailEvidence` as data, never queries anything, and is asserted (by a test)
never to fetch CloudTrail.

The reader was left to a later milestone, and the gap is the subject of this ADR.
Until it exists, the judge has no production source of evidence: the one caller
that could supply `CloudTrailEvidence` is the operator, which is exactly the
self-report the ADR exists to avoid.

The temptation to close the gap by having the judge query as well is the reason
ADR 0011 kept them apart. A module that both queried and judged could retry,
widen the window, or drop events, and none of that would appear in the verdict —
the retrieval step and its choices would be invisible inside a function that is
read as "the reconciliation".

M16 therefore adds the reader as its own module, and nothing else.

## Decision: `sws_agent.cloudtrail_reader`

New module. `LookupEventsEvidenceReader` performs a single logical
`cloudtrail:LookupEvents` operation, drains every page, and returns
`CloudTrailEvidence`. The reconciler is unchanged. The transport stays behind an
injected client seam, exactly as `sws_agent.ec2_observation` keeps it, so the
module imports no AWS SDK and the hermetic suite exercises it with a local fake.

### One `LookupAttributes` entry, and which attribute it is

`LookupEvents` accepts **only one** `LookupAttributes` key/value pair per call
(plus `StartTime`/`EndTime` and pagination). So the two read-time attributes ADR
0011 assigns to the reader cannot both be query attributes. The reader queries
`eventName = StopInstances` and applies `eventSource = ec2.amazonaws.com` in code,
skipping any record that does not carry both. Both are still applied *by the
reader*; the split between query and code is an API detail, not a division of
responsibility.

Records are filtered on their raw `EventName`/`EventSource` **before**
normalization, so unrelated traffic is skipped rather than mistaken for a
malformed record. A record that does claim to be an EC2 `StopInstances` event and
cannot then be read is a normalization failure and fails the whole query closed:
silently dropping it could undercount and manufacture a confirmation.

### The reader returns every matching event, and attributes none

Principal, session, target, and window are `reconcile_dispatch`'s four attributes.
The reader does not apply them. It returns **every** in-window EC2
`StopInstances` event so the reconciler can see the ambiguity it exists to catch —
two events, a different principal, a different target. A reader that selected one
"best" event would hide exactly the cases the judge is built to fail closed on,
which is why selection is not the reader's job.

This is also why a per-event "wrong target" is not a reader error. The reader
returns it; the reconciler refuses to count it. The division is the ADR 0011 split
made concrete.

### Field fidelity: the request, not the summary

`LookupEvents` returns `CloudTrailEvent` as a JSON **string** wrapping the full
record. The authoritative statement of what was *requested* is
`requestParameters.instancesSet.items[].instanceId`; the enclosing `Resources`
list is a summary of affected resources, used only as a fallback. When they
disagree the request wins — the reader normalizes the request, not the summary.

The session name is recovered from `Username`'s `ROLE_ID:session` form (falling
back to the last `/`-segment of the assumed-role ARN); `readOnly` is read from the
payload, falling back to the record's top-level `ReadOnly` string. A record that
yields no instance id at all is unusable: a `StopInstances` record whose targets
cannot be established is not evidence about *this* target.

### A failure is never an absence

Every way the query can fail becomes `CloudTrailEvidence(available=False, …)`:

| Failure | Result |
| --- | --- |
| lookup or pagination raises (denied, throttled, timeout) | `available=False`, error is the exception **type name only** |
| a page is not an object, or `Events` is malformed | `available=False` |
| a `StopInstances` record cannot be normalized | `available=False` |
| the window is missing, naive, or inverted | `available=False`, and no query is issued |

This is the whole point. "The query returned no events" and "the query never ran"
both present an empty tuple, and conflating them is the "probably fine" downgrade
ADR 0011 forbids. `available` is the field that keeps them apart, and the reader
sets it honestly.

The AWS exception's own text is deliberately **not** propagated: it can carry
request identifiers and request detail that must not reach a durable record. Only
the exception's type name is carried, which is enough to report the failure kind.

A window that is not timezone-aware, or not ordered, is refused **before** any
query is issued: a naive timestamp cannot be correlated, and assuming UTC for it
would be a guess in the one place a guess is least affordable.

### The reader names itself

The reader requires a non-blank `reader_principal_arn` and records it on the
evidence. `reconcile_dispatch`'s `evidence_provenance` check refuses evidence
whose reader is the mutating principal — a mechanical expression of the rule that
the credential which can change things must not be able to inspect itself. A
reader that could read anonymously, or label its evidence as someone else, would
defeat that check.

### What the reader deliberately does not do

- **It does not retry, and does not enforce delivery maturity.** CloudTrail
  delivery is asynchronous, so an early query can return a clean empty result for
  a dispatch that was processed. The runbook's Policy A (bounded wait and
  re-query) is procedural and lives in runbook §12 and decision record 0012. A
  retry loop in the reader would make it quietly authoritative about timing and
  would contradict ADR 0011's insistence that retrieval and judgement stay
  separate and visible.
- **It does not attribute or decide.** See above.
- **It does not dispatch.** There is no path here that calls `StopInstances` (or
  any mutation), builds a mutating client, or imports a mutating module.
- **It imports no AWS SDK.** `boto3`/`botocore` stay behind the injected seam.
- **It has no MCP surface.** It is not reachable from a tool, and MCP remains at
  nine tools.
- **It does not provision itself.** The forensic role, the lookup region, and the
  trail identity remain environment provisioning steps fixed by decision record
  0012 (Decision 4); the reader takes the region as a required constructor
  argument because `LookupEvents` is regional and a wrong-region query returns
  clean and empty forever.

### Seam contract

The injected `client` exposes `lookup_events()` returning the
`cloudtrail:LookupEvents` paginator, called as `paginate(**params)` and drained
like any other SWS read. A raw boto3 client exposes `get_paginator("lookup_events")`
instead, so the composition root supplies the one-method adapter — the same shape
`sws_live` uses for `describe_instances`. Keeping the adapter out of the module is
what keeps the module free of the SDK and testable without it.

## Wiring: reader into reconciler, proven in tests

The reader's output is fed straight into `reconcile_dispatch` in the tests, which
is the whole point of the split:

- a matching event → `CONFIRMED`
- a non-EC2 or non-`StopInstances` record (filtered by the reader) → empty →
  `AWS_SIDE_DISAGREEMENT` against a witness that claims a dispatch
- an event for another target, principal, or session → returned by the reader,
  refused by the reconciler → `AWS_SIDE_DISAGREEMENT`
- two matching events → returned by the reader, failed closed by the reconciler
- a wrong-region client → available and empty → `AWS_SIDE_DISAGREEMENT`, not a pass
- a failed lookup → `available=False` → `UNRESOLVED`, never `NO_DISPATCH_OBSERVED`

The live wiring (a composition root that constructs the forensic reader and feeds
`sws_live`) is **not** added here. The reader is deliberately standalone; the
provisioning and the composition that would use it remain open under the current
decision-record gates. What M16 establishes is that the reader half exists, is
read-only, fails closed, and produces evidence the existing judge accepts.

## Disposition for SWS v1 (frozen)

This ADR is ratified as the v1 architectural decision, and the decision is
**separation**:

- `sws_agent.cloudtrail_reader` is the independent **forensic evidence reader**.
- `sws_agent.reconciliation` is the deterministic **forensic adjudication layer**.
- They are **deliberately not wired into the live mutation execution path in
  SWS v1**. No production module constructs the reader, and no production module
  calls `reconcile_dispatch`; both are exercised through the independent forensic
  verification procedure in the runbook (section 12) and by the hermetic suite.
- Live execution therefore does **not** claim automatic CloudTrail verification
  after a mutation. Post-dispatch corroboration is a procedural runbook step, not
  an automatic stage of the live path.
- This is a **deliberate v1 boundary, not an unfinished implementation**. The
  reader and judge are complete and tested; what is intentional is that they are
  not composed into `sws_live`.
- Any future integration (a composition root that constructs the forensic reader
  and feeds `sws_live`) is **outside the frozen v1 scope** and would require a new,
  separately reviewed decision. This ADR does not create or schedule it.

## Exposure

The mutation stays **private and local**. M16 adds no MCP tool, no execute/apply
surface, and no mutation; MCP remains at **nine tools**. `PotentialAction.STOP_RESOURCE`
remains `implemented=False`. The reader is read-only by construction: a hermetic
test parses its AST and asserts it contains no mutating operation and no SDK
import, and that neither the MCP server nor `sws_agent.preflight` references it.

## Testing

`tests/test_m16_cloudtrail_reader.py`, hermetic: the paginator is a local fake and
the records are the shapes the API reference documents. No AWS call, socket, or
credential is involved.

The falsification tests, rather than coverage tests, are:

1. **Failure is not absence** — a raised lookup is `available=False` and
   `UNRESOLVED` with `retry_permissible=False`, distinguishable from an empty
   result.
2. **Ambiguity is preserved** — two matching events are returned by the reader and
   failed closed by the reconciler; the reader never picks one.
3. **The boundary holds** — an AST check asserts no mutating call and no SDK
   import; a source check asserts the reader is unreachable from MCP and the
   pre-deployment gate.

A malformed-`CloudTrailEvent` test and a missing-`EventTime` test pin the
fail-closed normalization, and a request-vs-`Resources` disagreement test pins the
authoritative source for the target set.
