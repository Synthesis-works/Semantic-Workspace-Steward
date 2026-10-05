"""The production EC2 mutation client, and the IAM artifact that bounds it.

This is the only module in SWS that is allowed to construct a client capable of
mutating AWS. It sits deliberately on one side of the hermeticity boundary
described in ADR 0009:

- ``sws_agent.ec2_mutation_client`` (this module) may import boto3/botocore.
- ``sws_agent.execution`` and ``sws_agent.ec2_mutation`` never import the SDK,
  must not import this module, and are tested with plain fakes.

The requirement this module exists to satisfy is that the production factory
establishes the *same* safety properties M15-D proves for an injected client.
It is not a weaker path:

**Retries.** The client's botocore config sets
``retries={"total_max_attempts": 1, "mode": "standard"}``. ``total_max_attempts``
is used rather than the legacy ``max_attempts`` because botocore normalizes the
legacy key as ``total_max_attempts = max_attempts + 1`` (``botocore/args.py``
600-621), so ``max_attempts=1`` is one *retry*. ``mode`` is stated explicitly so
the retry rules cannot be inherited from an ambient ``retry_mode`` setting.

Setting the value is not treated as sufficient. :func:`effective_retries` reads
the built client's own configuration back, and ``StopInstancesHandler`` runs its
precondition again on the result. The handler check is not redundant with the
factory: the factory is a supply-chain property, the handler check is a
consumer-side one, and this module's client can still be reconfigured after it is
built.

**Credentials.** Credentials and region are required fields, passed explicitly
to ``session.client(...)``. botocore's default credential chain is never
consulted, because the explicit values short-circuit it. There is no ``profile``
parameter: the read-only collector profile is not a fallback here, by
construction rather than by a check that could be forgotten. Absent or blank
credentials or region raise :class:`MutationClientConfigurationError` rather
than producing a client with implicit behaviour.

**Blast radius.** The handler limits ``InstanceIds`` to one element; the policy
generated here limits the resource. Both read the same
``MutationClientSettings.target_instance_arn`` so they cannot drift apart, and
:func:`validate_iam_policy` re-checks the finished policy as a plain dict, so a
hand-edited artifact that widened itself is rejected instead of trusted.

Nothing here is wired into a composition, ``STOP_RESOURCE.implemented`` remains
``False``, and no code path in this module calls ``stop_instances``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sws_agent.ec2_mutation import MutationClientConfigurationError

__all__ = [
    "MutationClientSettings",
    "MutationCredentials",
    "MutationClientFactory",
    "effective_retries",
    "mutation_iam_policy",
    "validate_iam_policy",
    "IAM_POLICY_VERSION",
]

IAM_POLICY_VERSION = "2012-10-17"

#: The one EC2 instance ARN format, restricted to a real region and account.
#: Written out rather than pattern-matched loosely because this string ends up
#: as a ``Resource`` in an IAM policy, where a permissive pattern would be a
#: policy bug rather than a code bug.
_INSTANCE_ARN = re.compile(
    r"^arn:aws(?:-[a-z]+)?:ec2:[a-z0-9-]+:\d{12}:instance/i-[0-9a-f]{8,17}$"
)

#: The only two actions the mutation identity may hold.
_ALLOWED_ACTIONS = frozenset({"ec2:StopInstances", "ec2:DescribeInstances"})


@dataclass(frozen=True)
class MutationCredentials:
    """Credentials for the mutation identity, with a recorded origin.

    ``source`` is not used to build anything; it exists so an audit can say where
    the credential came from instead of inferring it. A credential whose origin
    is unknown is not evidence.
    """

    access_key_id: str
    secret_access_key: str
    session_token: str | None = None
    source: str = "explicit"

    def __post_init__(self) -> None:
        for name in ("access_key_id", "secret_access_key", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise MutationClientConfigurationError(
                    f"MutationCredentials.{name} is required and may not be "
                    f"blank; the mutation client refuses to fall back to an "
                    f"ambient credential source"
                )

    def boto3_kwargs(self) -> dict[str, str]:
        """Explicit credential kwargs for ``session.client(...)``.

        Returning the keyword arguments rather than letting boto3 discover
        credentials is what makes the ambient chain unreachable here.
        """
        kwargs = {
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
        }
        if self.session_token:
            kwargs["aws_session_token"] = self.session_token
        return kwargs


@dataclass(frozen=True)
class MutationClientSettings:
    """Everything needed to build the mutation client, and nothing ambient.

    ``target_instance_arn`` is required even though the client does not send it,
    because it is the shared source of truth for the handler's one-target check
    and the IAM policy's resource. If it were optional, the two could drift.
    """

    region: str
    credentials: MutationCredentials
    target_instance_arn: str
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.region, str) or not self.region.strip():
            raise MutationClientConfigurationError(
                "MutationClientSettings.region is required and may not be blank; "
                "an unbound region would let an ambient default decide which "
                "endpoint a mutation is sent to"
            )
        if not isinstance(self.credentials, MutationCredentials):
            raise MutationClientConfigurationError(
                "MutationClientSettings.credentials must be a MutationCredentials "
                "with an explicit source; there is no ambient credential fallback"
            )
        arn = self.target_instance_arn
        if not isinstance(arn, str) or not _INSTANCE_ARN.match(arn.strip()):
            raise MutationClientConfigurationError(
                f"target_instance_arn is not a single EC2 instance ARN: {arn!r}. "
                "The mutation identity is scoped to exactly one instance, so the "
                "ARN is validated here rather than trusted."
            )
        for name in ("connect_timeout_seconds", "read_timeout_seconds"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise MutationClientConfigurationError(
                    f"{name} must be a positive integer, got {value!r}"
                )

    @property
    def instance_id(self) -> str:
        """The ``i-...`` id, for cross-checking against the handler's one target."""
        return self.target_instance_arn.rsplit("/", 1)[1]


def build_botocore_config(settings: MutationClientSettings) -> Any:
    """The botocore ``Config`` this factory will use, without building a client.

    Split out from :class:`MutationClientFactory` so the configuration is
    inspectable in a test with no SDK session, no credentials, and no AWS.
    """
    from botocore.config import Config  # type: ignore[import-untyped]  # lazy: SDK behind the boundary

    return Config(
        retries={"total_max_attempts": 1, "mode": "standard"},
        connect_timeout=settings.connect_timeout_seconds,
        read_timeout=settings.read_timeout_seconds,
    )


def effective_retries(client: Any) -> dict[str, Any]:
    """Read back the retries botocore actually resolved for ``client``.

    This is the "don't assume, verify" step. It reports the effective mapping
    including botocore's own normalization, so a caller can prove a retry is
    impossible without re-deriving the rules.
    """
    retries = getattr(getattr(getattr(client, "meta", None), "config", None), "retries", None)
    if not isinstance(retries, dict):
        raise MutationClientConfigurationError(
            "the built client exposes no readable retry configuration; SWS will "
            "not assume a retry is impossible"
        )
    return dict(retries)


class MutationClientFactory:
    """Builds the one EC2 client SWS is permitted to mutate with.

    ``session_factory`` receives no profile name and cannot be given one: the
    read-only collector's profile is not reachable from here. Tests inject a fake
    to inspect the call without a session.
    """

    def __init__(
        self,
        settings: MutationClientSettings,
        *,
        session_factory: Any = None,
    ) -> None:
        if not isinstance(settings, MutationClientSettings):
            raise MutationClientConfigurationError(
                "MutationClientFactory requires MutationClientSettings"
            )
        self._settings = settings
        self._session_factory = session_factory

    @property
    def settings(self) -> MutationClientSettings:
        return self._settings

    def botocore_config(self) -> Any:
        """The config a client from this factory will carry."""
        return build_botocore_config(self._settings)

    def _session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        import boto3  # type: ignore[import-untyped]  # lazy: the SDK stays behind the boundary

        # No profile_name= argument: constructing a default boto3 Session is
        # harmless here only because every credential is passed explicitly to
        # client(), so no part of the chain is consulted.
        return boto3.Session()

    def __call__(self) -> Any:
        """Build the mutation client.

        Constructing a boto3 client performs no AWS call, so this is safe to run
        in a test. It does not stop anything; that requires calling
        ``stop_instances`` on the result, which nothing in SWS does.
        """
        session = self._session()
        client = session.client(
            "ec2",
            region_name=self._settings.region.strip(),
            config=build_botocore_config(self._settings),
            **self._settings.credentials.boto3_kwargs(),
        )
        # Verify rather than assume: confirm the built client really has one
        # attempt, and that it is the ec2 service. A client that fails either
        # check never reaches the handler.
        retries = effective_retries(client)
        if retries.get("total_max_attempts") != 1:
            raise MutationClientConfigurationError(
                f"the mutation client resolved to total_max_attempts="
                f"{retries.get('total_max_attempts')!r} rather than 1; a mutation "
                "must dispatch exactly once, so this client is refused rather "
                "than handed to the handler"
            )
        service = getattr(getattr(client, "meta", None), "service_model", None)
        service_id = getattr(service, "service_id", None)
        if service_id is not None:
            # botocore exposes ``ServiceId``, which hyphenizes itself; a plain
            # string is accepted too so a stub or a future botocore shape does
            # not crash the check it exists to perform.
            resolved = (
                service_id.hyphenize()
                if hasattr(service_id, "hyphenize")
                else str(service_id)
            )
            if resolved != "ec2":
                raise MutationClientConfigurationError(
                    f"expected an ec2 client, got {resolved!r}; this factory "
                    "builds the mutation client and nothing else"
                )
        return client


def mutation_iam_policy(settings: MutationClientSettings) -> dict[str, Any]:
    """The IAM policy artifact bounding the mutation identity.

    ```text
    StopInstances:
      Resource = arn:aws:ec2:<region>:<account>:instance/<sacrificial-instance>

    DescribeInstances:
      Resource = *

    Reason:
      required for post-dispatch observation; EC2 does not provide
      an equivalent single-instance resource constraint for this API.
    ```

    The ``*`` on ``DescribeInstances`` was ratified by the owner at M15-E as an
    intentional, unavoidable **read-only overbreadth for this first mutation role
    only**. It is not an acceptable general pattern and must not be copied into a
    future role. The justification is the observation contract, not convenience:
    settlement reads post-state through this call, and EC2's resource-level
    authorization for it offers no usable single-instance constraint, so a
    narrower policy would leave the mutation unauditable.

    Ratifying that one grant authorizes no other EC2 mutation -- no terminate,
    reboot, modify, or start -- and no wildcard action. It is read-only, so its
    worst case is metadata disclosure rather than a change to infrastructure, and
    it cannot be removed without making every outcome unclassifiable.

    The result is an artifact for review. Nothing attaches it.
    """
    return {
        "Version": IAM_POLICY_VERSION,
        "Statement": [
            {
                "Sid": "StopSacrificialInstanceOnly",
                "Effect": "Allow",
                "Action": ["ec2:StopInstances"],
                "Resource": [settings.target_instance_arn],
            },
            {
                "Sid": "DescribeInstancesForSettlement",
                "Effect": "Allow",
                "Action": ["ec2:DescribeInstances"],
                # Unavoidable; see the docstring. The one place a wildcard is
                # permitted, and validate_iam_policy enforces that it is here.
                "Resource": "*",
            },
        ],
    }


def validate_iam_policy(policy: dict[str, Any], settings: MutationClientSettings) -> None:
    """Re-check a finished policy as plain data, and refuse a widened one.

    The generator is not trusted to be the only way this dict gets produced; an
    artifact edited by hand must not deploy silently. Every invariant the design
    relies on is asserted again here, against the dict.
    """
    if not isinstance(policy, dict):
        raise MutationClientConfigurationError("an IAM policy must be a dict")

    statements = policy.get("Statement")
    if not isinstance(statements, list) or not statements:
        raise MutationClientConfigurationError("an IAM policy needs a Statement list")

    seen_stop = False
    seen_describe = False
    for statement in statements:
        if not isinstance(statement, dict):
            raise MutationClientConfigurationError("each statement must be a dict")
        if statement.get("Effect") != "Allow":
            raise MutationClientConfigurationError(
                f"only Allow statements are expected, found "
                f"{statement.get('Effect')!r}"
            )
        actions = statement.get("Action")
        actions = [actions] if isinstance(actions, str) else actions
        if not isinstance(actions, list) or not actions:
            raise MutationClientConfigurationError("every statement needs an Action")
        for action in actions:
            if not isinstance(action, str):
                raise MutationClientConfigurationError(f"non-string action {action!r}")
            if "*" in action or "?" in action:
                raise MutationClientConfigurationError(
                    f"wildcard action {action!r} is refused; the mutation identity "
                    f"holds {sorted(_ALLOWED_ACTIONS)} and nothing else"
                )
            if action not in _ALLOWED_ACTIONS:
                raise MutationClientConfigurationError(
                    f"action {action!r} is outside the mutation identity's scope "
                    f"({sorted(_ALLOWED_ACTIONS)}); in particular no terminate, "
                    f"reboot, or modify permission is acceptable"
                )

        resource = statement.get("Resource")
        resources = [resource] if isinstance(resource, str) else resource
        if not isinstance(resources, list) or not resources:
            raise MutationClientConfigurationError("every statement needs a Resource")
        for entry in resources:
            if not isinstance(entry, str):
                raise MutationClientConfigurationError(f"non-string resource {entry!r}")

        if actions == ["ec2:StopInstances"]:
            seen_stop = True
            if resources != [settings.target_instance_arn]:
                raise MutationClientConfigurationError(
                    f"ec2:StopInstances must be scoped to exactly "
                    f"[{settings.target_instance_arn!r}], got {resources!r}"
                )
        elif actions == ["ec2:DescribeInstances"]:
            seen_describe = True
            if resources != ["*"]:
                raise MutationClientConfigurationError(
                    "ec2:DescribeInstances is the one unavoidable wildcard, but "
                    f"it must be the only resource in its statement, got {resources!r}"
                )
        else:
            raise MutationClientConfigurationError(
                f"unexpected action grouping {actions!r}"
            )

    if not seen_stop:
        raise MutationClientConfigurationError(
            "the policy has no ec2:StopInstances statement, so it could not stop "
            "the sacrificial instance"
        )
    if not seen_describe:
        raise MutationClientConfigurationError(
            "the policy has no ec2:DescribeInstances statement, so settlement "
            "could not read post-state and classify an outcome"
        )
    if policy.get("Version") != IAM_POLICY_VERSION:
        raise MutationClientConfigurationError(
            f"unexpected policy Version {policy.get('Version')!r}"
        )
