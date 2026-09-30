# ADR 0002: EC2 observation identity comes from the read, not from configuration

- **Status**: Accepted
- **Date**: 2026
- **Scope**: how the M11 read-only EC2 observation provider establishes which
  instance, in which account, in which region, it actually read

## Context

M10 hardened the A5 execution gate so that evidence is obtained by the
coordinator itself, is freshness-bounded on both sides, must match the target
resource's identity, and can only be satisfied by a postcondition derived from
the action rather than by the caller. M11 supplies the read that produces that
evidence. The provider had to answer four questions honestly, and none of them
had an obvious answer.

`ec2:DescribeInstances` returns no ARN. So the observation's `arn` — which M10
now compares against the snapshot record — has to be constructed locally. A
constructed ARN is weaker evidence than an observed one, and pretending
otherwise would be exactly the kind of plausible-looking fabrication M10 was
written to remove.

The account is the harder problem. M10's identity gate compares
`observation.account_id` against the snapshot record's `account_id`, so if the
provider simply echoed a configured account, the check would be circular: it
would compare configuration to configuration and report agreement while proving
nothing about the instance. Two real sources were available, and both were
rejected for different reasons.

`sts:GetCallerIdentity` returns the account of the *credentials*, which is the
wrong question whenever a cross-account role is in play: the caller's account
and the instance owner's account are then different values, and using the
credentials' account would silently bless a cross-account target. It also costs
an extra API call and an extra IAM permission to obtain information the EC2
response already contains.

Taking the account from configuration is what M10 exists to distrust, as noted
above.

What EC2 actually returns is the enclosing reservation's `OwnerId`: the account
that owns the instance, from the same response, in the same call. It is
observed rather than asserted, and it is the correct question.

`region` and `partition` are different in kind: neither is returned by the API,
and both are deployment facts rather than claims about the target. They are
bound explicitly when the provider is constructed.

## Decision

`Reservation.OwnerId` from the `DescribeInstances` response is the **only**
source of `account_id`. It is never read from configuration, never taken from a
caller, and never augmented with an STS call. A configured account, if one is
ever introduced, may serve only as a precondition that *refuses* on mismatch —
never as the value reported as observed evidence.

`region` and `partition` are explicit provider-construction bindings. The
partition is **never** inferred from the region string; that would be a new,
silent derivation rule with no single owner and no test that could pin its
edge cases.

The `arn` is therefore **constructed**:

```text
arn:{partition}:ec2:{region}:{account_id}:instance/{instance_id}
```

This has a consequence that must be stated rather than glossed over: because the
ARN is built from the same components the gate compares, M10's ARN equality
check is a **consistency check over identity components** — it catches region or
account drift between snapshot time and observation time — and **not**
independent verification of the ARN string. The genuinely independent evidence
in an M11 observation is the instance state and the reservation owner.

`State.Name` is the independently observed postcondition fact, and it is passed
through byte-for-byte. It is never lowercased, cased, mapped through aliases,
or translated, because M10 compares it by exact string equality against
`{"state": "stopped"}`. A provider that "helpfully" normalized the value would
either break a correct verification or — worse — be tuned until the comparison
passed. A recently terminated instance reports `terminated`, which never equals
`stopped`, so that visibility window cannot produce a false success.

The instance id in the observation is the one **AWS returned**, never the id
that was requested. If a read ever resolved to a different instance than the
caller asked for, M10's identity gate is what must catch it; a provider that
echoed the requested id would erase the only signal that could.

## Consequences

- The provider needs no account configuration, no STS call, and no IAM
  permission beyond `ec2:DescribeInstances`. Nothing in this design can be
  satisfied by a plausible-looking configuration value.
- Zero, multiple, missing, or malformed results raise `ObservationError`, which
  M10 already treats fail-closed. No AWS outcome is interpretable as "absent,
  therefore safe to stop": an invalid instance id, a recently terminated
  instance, and an instance owned by another account all look like "no
  results".
- SWS calls `ec2:DescribeInstances` only. There is no generic EC2 dispatch and
  no mutation method on the client seam, so `ec2:StopInstances` is unreachable
  from SWS. M11 remains read-only and `PotentialAction.STOP_RESOURCE` remains
  `implemented=False`.
- Because the EC2 API is eventually consistent, a read taken immediately after
  a stop may still report the prior state. M10 maps a contradicted fact to a
  failed attempt, which is safe (never a false success) but can report a false
  failure during a propagation window. Bounded polling for that window belongs
  to the milestone that owns the mutation, not to M11.
- Future work that needs an EC2 *inventory* collector will have to decide where
  a record's `account_id` comes from, since a collector gathering many
  instances spans accounts. That decision is deliberately not made here, and
  the same circularity warning applies to it.

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| `sts:GetCallerIdentity` | Answers the credential's account, not the instance owner's; wrong under cross-account roles. Costs an extra call and IAM permission for information already in the response. |
| Configured account, echoed as observed | Makes the M10 account comparison circular — configuration versus configuration. |
| `owner-id` filter to constrain results | A filter returns an empty result for a non-owned instance, which is indistinguishable from "does not exist". It would hide the cross-account case rather than report it. |
| Infer the partition from the region | A new silent derivation rule with unclear edge cases, and the partition is a deployment fact that belongs in a binding. |
