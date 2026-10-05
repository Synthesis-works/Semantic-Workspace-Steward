# ADR 0009: Production mutation client factory and scoped IAM artifact

- Status: accepted (design and implementation only; no live execution)
- Milestone: M15-E — accepted, with the owner ratifying the `ec2:DescribeInstances`
  on `*` grant for this first mutation role only (section 6).
- Date: 2026-10-04
- Depends on: ADR 0006 (dispatch evidence and settlement), ADR 0008 (handler and settlement)

## Context

M15-D built `StopInstancesHandler` against an *injected* client and proved, on a
fake, that a client which can retry is refused. That proof is only worth as much
as the weakest path that can produce a client. Until now nothing in the repository
could produce a real one: `sws_agent.aws.AwsClientFactory` builds `AwsMultiClient`,
a read-only collector, and it deliberately enables retries
(`aws.py:226-230`). Feeding it to a mutation handler would be refused today, which
is the right outcome but the wrong reason to stop.

M15-E adds the production client. Its central requirement:

> The production client factory must establish the same safety properties that
> M15-D proves for an injected client.

The factory must never be a weaker path than the tested seam.

## A defect found while doing this

While designing the factory's retry configuration, M15-E checked botocore's
actual retry normalization instead of trusting key names. `botocore.args`
(verified on 1.43.101, `args.py:600-621`) computes:

```python
if "total_max_attempts" in retries:
    retries.pop("max_attempts", None)
    return
if "max_attempts" in retries:
    value = retries.pop("max_attempts")
    # client config max_attempts means total retries so we have to add one
    retries["total_max_attempts"] = value + 1
```

So the two keys are not synonyms. `total_max_attempts` counts the initial
request; `max_attempts` counts retries *after* it. Measured on real clients:

| `retries` passed          | effective attempts | retries |
|---------------------------|--------------------|---------|
| `total_max_attempts: 1`   | 1                  | none    |
| `max_attempts: 0`         | 1                  | none    |
| `max_attempts: 1`         | **2**              | **one** |
| `max_attempts: 3`         | 4                  | three   |
| `{}` / absent             | **3**              | **two** |

M15-D's precondition accepted either key with value `1`, and had a test named
`test_a_legacy_max_attempts_key_of_one_is_accepted` whose docstring asserted the
legacy key "means the same thing". That test was wrong, and it encoded the
wrongness as if it were a guarantee. The rule is unreachable for any client
botocore built itself, because botocore always normalizes to
`total_max_attempts` — which is why the M15-D gate passed while the hazard was
real. It is reachable for exactly the clients the precondition exists to police:
injected stubs, third-party clients, or a config mutated after construction.

Two consequences, both adopted:

1. The precondition accepts `total_max_attempts=1` and `max_attempts=0`, and
   refuses `max_attempts=1`.
2. An absent or empty `retries` mapping is refused, because botocore substitutes
   `DEFAULT_MAX_ATTEMPTS` (3) and retries silently. "Unconfigured" is not
   "disabled", and this is the strongest argument for the factory setting the
   value explicitly rather than relying on a default.

The deeper lesson is recorded as a rule rather than a fix: a safety test that
shares an assumption with the code it tests cannot catch that assumption. The
normalization fact is now pinned by a test that calls botocore, so a botocore
upgrade that changes it fails loudly instead of quietly.

## Decision

### 1. A dedicated module, not a general EC2 accessor

New module `sws_agent.ec2_mutation_client`. It does not import
`AwsClientFactory`, does not accept a `profile`, and exposes no
`get_ec2_client()`-shaped escape hatch. The only thing it builds is a client
bound to one sacrificial instance ARN. `AwsMultiClient` stays read-only and
keeps its retries.

### 2. Explicit credentials, no ambient chain

`MutationClientSettings` requires an access key, secret, and region as fields.
The factory passes them to `session.client(...)` explicitly, so botocore never
consults the default credential chain. `MutationCredentials` requires a
`source` string recorded in provenance, so a credential's origin is auditable
rather than inferred.

Absent or blank region or credentials raise `MutationClientConfigurationError`.
There is no profile field: the read-only profile is not a fallback, by
construction rather than by check.

### 3. Retries disabled explicitly, then verified

The factory sets `retries={"total_max_attempts": 1, "mode": "standard"}`. Two
independent reasons this is not sufficient on its own, both required:

- `mode: standard` is stated explicitly so the retry *rules* are fixed and
  cannot drift with an ambient `retry_mode` setting.
- The resulting client's effective configuration is read back from
  `client.meta.config.retries` before the handler sees it.

`effective_retries()` is a module-level function so the configuration is
inspectable without contacting AWS; the factory exposes `build_botocore_config()`
separately from `__call__()`, so a test can assert the configuration and then
assert the client, with no network in either step.

### 4. Defense in depth, not defense in depth alone

The handler still runs its own precondition on every client it receives,
including one the factory built. Factory correctness is a supply-chain
property; the handler check is a consumer-side property. Removing either leaves
the other. The M15-E tests exercise both, including a mutation-client produced
by the factory that is then reconfigured to retry and is refused by the handler.

### 5. Blast radius is constrained twice, independently

The handler constrains `InstanceIds` to one element. IAM constrains the
resource. These are independent: a handler bug cannot widen the policy, and a
policy bug cannot widen the handler. Both derive the ARN from the same
`MutationClientSettings.target_instance_arn` so the two cannot drift apart, and
`validate_iam_policy()` re-checks the artifact as a dict, so a hand-edited
policy that widened itself is rejected rather than deployed.

### 6. The IAM artifact

```text
StopInstances:
  Resource = arn:aws:ec2:<region>:<account>:instance/<sacrificial-instance>

DescribeInstances:
  Resource = *

Reason:
  required for post-dispatch observation; EC2 does not provide
  an equivalent single-instance resource constraint for this API.
```

**RATIFIED BY THE OWNER, M15-E acceptance.** The `DescribeInstances` grant on `*`
is an **intentional, unavoidable read-only overbreadth for this first mutation
role specifically**. It is not an acceptable general pattern and must not be
copied into any future role.

The reason is the current architecture's actual observation contract, not
convenience. `DescribeInstances` establishes post-dispatch state, and EC2's
resource-level authorization for this API does not offer a usable single-instance
constraint equivalent to the `StopInstances` ARN restriction. Narrowing the
policy syntactically while making the observation path fail would be security
theater: the mutation would become unauditable, and the "did it actually stop?"
question would lose its only evidence source.

**This ratifies that one permission for that one role. It authorizes no other EC2
mutation.** No terminate, reboot, modify, or start permission is granted or
implied, and no wildcard action exists. `validate_iam_policy()` continues to
enforce that: the permitted action set is exactly
`{ec2:StopInstances, ec2:DescribeInstances}`, and any other action is refused.

Two further properties hold, and they are what keep the overbreadth bounded:

- It is read-only. It cannot mutate anything, so its worst case is disclosure of
  instance metadata within the account, not a change to infrastructure.
- Settlement cannot classify an outcome without it. `pending` is unmapped in the
  post-state contract (ADR 0006), and `running`/`stopping`/`stopped` all come
  from this call, so removing it would make every outcome `OUTCOME_UNKNOWN`.

The policy is an artifact only. It is not attached, and attaching it belongs to
the first live stop milestone.

### 7. Hermeticity boundary

`sws_agent.ec2_mutation_client` may import boto3/botocore, as `sws_agent.aws`
already does. `execution.py` and `ec2_mutation.py` do not import the SDK and
must not: `ec2_mutation.py` stays SDK-free so the handler's precondition is
testable with a plain fake and so nothing in the core can acquire credentials.
M15-E tests assert this by inspecting `sys.modules` after importing the core
modules.

## Consequences

- `STOP_RESOURCE.implemented` stays `False`. The factory existing does not
  enable the action, and the factory is not wired into any composition.
- The IAM policy is not attached; it is reviewed text.
- `sws_agent.aws` is untouched, so the read path keeps its retries.
- A botocore upgrade that alters retry normalization now fails a test instead of
  silently changing the safety property.

## Explicitly not decided here

- Attaching the policy, creating the role, installing credentials, and the
  first live `StopInstances`. These belong to the first-live-stop milestone,
  which needs its own preflight, abort criteria, and post-execution review.
- Credential rotation and how the role is assumed at runtime.
- Whether the role is attached to a task role, an instance profile, or
  `sts:AssumeRole` from an operator session.
