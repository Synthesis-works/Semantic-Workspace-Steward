"""Read-only EC2 instance observation provider (M11).

M11 supplies exactly one thing: the *read* half of the boundary that M10's
``ObservationProvider`` protocol and A5 execution gate already define. It adds
no mutation, no mutation handler, no execution path, and no MCP tool --
``PotentialAction.STOP_RESOURCE`` remains ``implemented=False``.

The provider is deliberately pessimistic. It observes **one** instance with
one logical ``ec2:DescribeInstances`` call, and it would rather raise
``ObservationError`` than return anything it cannot firmly establish:

- zero instances -> error (never "absent, therefore safe")
- more than one instance -> error (never "pick the first one")
- a missing/blank ``InstanceId``, ``State.Name``, or ``OwnerId`` -> error
- any AWS/transport failure -> error (no raw AWS text is propagated)

``ObservationError`` is exactly what the coordinator already handles
fail-closed (preflight ``OBSERVATION_PROVIDER_UNAVAILABLE``; post-attempt
UNKNOWN), so no M10 semantic is changed to make this provider safe.

Identity honesty (the core design, recorded in ADR 0002):

- ``resource_id`` is the instance id **AWS returned**, never the requested id.
  A mismatch therefore surfaces at the M10 identity gate instead of being
  silently laundered into a claim about a different instance.
- ``account_id`` is the enclosing reservation's ``OwnerId`` -- the instance
  owner's account, observed in the same read. It is never taken from
  configuration and never from a caller, because a configured account echoed
  back as "observed evidence" would make the M10 account comparison circular.
  STS is deliberately not called: ``GetCallerIdentity`` answers a different
  question (the *credential's* account) and would add a call plus an IAM
  permission for no gain.
- ``region`` and ``partition`` are deployment facts, bound explicitly at
  construction. ``DescribeInstances`` returns neither, and the partition is
  never inferred from the region string (that would be a new, silent
  derivation rule).
- ``arn`` is therefore *constructed* rather than observed, because
  ``DescribeInstances`` returns no ARN. It is a consistency check over the
  identity components above, not independent ARN evidence.

The postcondition fact is the one genuinely independent piece of evidence:
``State.Name`` is passed through **byte-for-byte**, never lowercased,
cased, mapped through aliases, or translated. M10 compares it by exact string
equality against ``{"state": "stopped"}``, so any normalization here would
either break a correct verification or, worse, be tuned to make a comparison
succeed.

Boundaries:

- This module never imports ``boto3`` or ``botocore`` and never performs an
  AWS call itself; the client seam is injected (``aws.AwsMultiClient``). The
  provider is not constructed by the MCP backend and is not reachable from any
  tool.
- The provider implements no retry loop. The AWS SDK's own retry/timeout
  behavior (configured once in ``aws.AwsClientFactory``) is preserved, and a
  second retry layer here would be an unobservable, unaudited amplifier.
- ``observed_at`` is always stamped by the injected clock immediately after
  the response has been fully drained. It is never an input, so no caller can
  supply, back-date, or age its own evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ._identity import utc_now
from .constants import SWSResourceType
from .models import ResourceObservation
from .verification import ObservationError

DEFAULT_PARTITION: str = "aws"
"""The commercial AWS partition, the explicit default for ARN construction."""

_ACCOUNT_ID_LENGTH: int = 12
"""AWS account ids are exactly 12 digits (``ResourceObservation`` enforces it)."""

# Only the optional, human-useful attributes M11 is authorized to surface.
EC2_OPTIONAL_FACT_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("instance_type", ("InstanceType",)),
    ("launch_time", ("LaunchTime",)),
    ("private_ip_address", ("PrivateIpAddress",)),
    ("vpc_id", ("VpcId",)),
    ("subnet_id", ("SubnetId",)),
    ("availability_zone", ("Placement", "AvailabilityZone")),
)
"""The single approved list of optional EC2 instance attributes.

Public, and the only place this list exists: M13-A's inventory collector
(``inventory.Ec2InstanceCollector``) reads the same ``ec2:DescribeInstances``
response and must surface exactly the same attributes, so keeping one list here
is what stops the two readers of that response from drifting into different
vocabularies for the same resource.
"""


def _require_text(value: Any, label: str) -> str:
    """Return ``value`` when it is a non-blank string, else raise."""
    if not isinstance(value, str) or not value.strip():
        raise ObservationError(
            f"EC2 observation could not establish {label}: "
            f"expected a non-empty string, got {type(value).__name__}"
        )
    return value


def _dig(source: Any, path: tuple[str, ...]) -> Any:
    """Walk a nested response path, tolerating absent intermediate nodes."""
    current = source
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def ec2_instance_facts(instance: dict[str, Any]) -> dict[str, Any]:
    """Collect the optional, observability-only facts that AWS actually sent.

    Nothing is defaulted or invented: an attribute AWS omitted is simply
    absent from the result. ``LaunchTime`` arrives as a ``datetime`` from
    boto3 and is rendered as ISO-8601 so the facts stay JSON-safe in the audit
    ledger; that is a lossless rendering, not a derived claim.

    ``instance_state`` is deliberately *not* included: this helper is about
    the non-state attributes, and each caller already reads the state itself
    under its own honesty rules (this provider requires it; the M13-A collector
    records what was sent). Folding it in here would give one caller a second,
    silently relaxed path to the same fact.
    """
    facts: dict[str, Any] = {}
    for name, path in EC2_OPTIONAL_FACT_PATHS:
        value = _dig(instance, path)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, datetime):
            value = value.isoformat()
        facts[name] = value
    return facts


class Ec2InstanceObservationProvider:
    """Observes one EC2 instance's authoritative state, read-only.

    Constructed with an injected client seam exposing ``describe_instances()``
    (which returns the ``ec2:DescribeInstances`` paginator), the region and
    partition to bind identity against, and optionally an injected clock.

    The instance is a per-call argument, not a construction-time binding: the
    returned observation always reports the id AWS actually returned, so M10's
    identity gate can detect a wrong-target observation.
    """

    def __init__(
        self,
        client: Any,
        *,
        region: str,
        partition: str = DEFAULT_PARTITION,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(region, str) or not region.strip():
            raise ValueError("EC2 observation requires an explicit region binding")
        if not isinstance(partition, str) or not partition.strip():
            raise ValueError("EC2 observation requires an explicit partition binding")
        self._client = client
        self._region = region
        self._partition = partition
        self._now: Callable[[], datetime] = now or utc_now

    def observe(self, resource_id: str) -> ResourceObservation:
        """Observe one instance, or raise ``ObservationError``.

        Never returns ``None``: there is no "no observation available" case
        here -- either the instance's state was established from AWS, or the
        provider raises.
        """
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ObservationError(
                "EC2 observation requires a non-empty instance id, got "
                f"{type(resource_id).__name__}"
            )

        instance, owner_id = self._read_single_instance(resource_id)

        instance_id = _require_text(instance.get("InstanceId"), "an instance id")
        state = _require_text(
            _dig(instance, ("State", "Name")), "an authoritative instance state"
        )
        if not (
            isinstance(owner_id, str)
            and len(owner_id) == _ACCOUNT_ID_LENGTH
            and owner_id.isdigit()
        ):
            raise ObservationError(
                "EC2 observation could not establish an account identity: the "
                "DescribeInstances reservation did not report a 12-digit "
                "OwnerId"
            )

        # Stamped only after the response is fully drained: the timestamp must
        # describe when the state was actually read.
        observed_at = self._now()

        facts: dict[str, Any] = {"state": state}
        facts.update(ec2_instance_facts(instance))

        try:
            return ResourceObservation.issued(
                resource_id=instance_id,
                resource_type=SWSResourceType.EC2_INSTANCE,
                facts=facts,
                observed_at=observed_at,
                ambiguous=False,
                arn=(
                    f"arn:{self._partition}:ec2:{self._region}:{owner_id}"
                    f":instance/{instance_id}"
                ),
                account_id=owner_id,
                region=self._region,
            )
        except ObservationError:
            raise
        except Exception as exc:  # pydantic ValidationError, and any model drift
            # A malformed/hostile response must fail closed as an observation
            # failure the coordinator already understands, never as an
            # unhandled exception and never as a valid-looking observation.
            raise ObservationError(
                "EC2 observation could not be recorded as valid evidence: "
                f"{type(exc).__name__}"
            ) from exc

    def _read_single_instance(self, resource_id: str) -> tuple[dict[str, Any], str]:
        """Return the single matching instance and its reservation owner id.

        Performs exactly one logical ``ec2:DescribeInstances`` operation and
        drains every page, so "one page" is never assumed. The enclosing
        reservation is retained because ``OwnerId`` -- the account identity
        source -- lives there, not on the instance.
        """
        try:
            paginator = self._client.describe_instances()
        except ObservationError:
            raise
        except Exception as exc:
            raise ObservationError(
                "EC2 observation could not obtain a DescribeInstances "
                f"paginator: {type(exc).__name__}"
            ) from exc

        found: list[tuple[dict[str, Any], str]] = []
        try:
            for page in paginator.paginate(InstanceIds=[resource_id]):
                if not isinstance(page, dict):
                    raise ObservationError(
                        "EC2 observation received a malformed DescribeInstances "
                        f"page: {type(page).__name__}"
                    )
                reservations = page.get("Reservations")
                if reservations is None:
                    continue
                if not isinstance(reservations, list):
                    raise ObservationError(
                        "EC2 observation received malformed Reservations: "
                        f"{type(reservations).__name__}"
                    )
                for reservation in reservations:
                    if not isinstance(reservation, dict):
                        raise ObservationError(
                            "EC2 observation received a malformed reservation: "
                            f"{type(reservation).__name__}"
                        )
                    owner_id = reservation.get("OwnerId")
                    instances = reservation.get("Instances")
                    if instances is None:
                        continue
                    if not isinstance(instances, list):
                        raise ObservationError(
                            "EC2 observation received malformed Instances: "
                            f"{type(instances).__name__}"
                        )
                    for instance in instances:
                        if not isinstance(instance, dict):
                            raise ObservationError(
                                "EC2 observation received a malformed instance: "
                                f"{type(instance).__name__}"
                            )
                        found.append((instance, owner_id))
        except ObservationError:
            raise
        except Exception as exc:
            # AccessDenied, UnauthorizedOperation, InvalidInstanceID.NotFound,
            # throttling, transport failures, and timeouts all land here. The
            # AWS exception's own message is intentionally not propagated: it
            # can carry request identifiers and request detail that must not
            # reach a durable record.
            raise ObservationError(
                "EC2 observation could not reach an authoritative state for "
                f"the instance: {type(exc).__name__}"
            ) from exc

        if not found:
            # Zero results is ambiguous by nature: the id may be invalid,
            # terminated past visibility, or owned by another account. It is
            # NEVER read as "gone, therefore safe to stop".
            raise ObservationError(
                "EC2 observation found no visible instance for the requested "
                "id; the state cannot be established (an invalid id, a "
                "recently terminated instance, and an instance owned by another "
                "account all look the same here)"
            )
        if len(found) > 1:
            raise ObservationError(
                "EC2 observation found "
                f"{len(found)} instances for one requested id; the target is "
                "ambiguous and no single one will be chosen"
            )
        return found[0]
