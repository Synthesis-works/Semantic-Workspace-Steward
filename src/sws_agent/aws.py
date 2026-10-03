"""Real AWS client factory for the existing SWS collectors (M6).

Fresh SWS module (no SMS reuse). This is the adapter between boto3 and the
already-existing collector client protocols (interfaces.InventoryCollector
and the Cost Explorer collector). It is infrastructure plumbing only: no
collector, policy, authorization, or approval logic lives here.

Scope and honesty (M6):

- boto3 stays optional and is imported lazily. Importing ``sws_agent.aws``
  (and the rest of ``sws_agent``) never imports boto3/botocore and never
  performs an AWS call. Clients are built only when the returned factory is
  invoked, which in the MCP backend happens at tool execution time.
- The factory consumes the existing ``AWSConnectionConfig`` (configured
  region/profile) and applies the canonical ``AWS_API_RETRY_ATTEMPTS`` and
  ``AWS_API_TIMEOUT_SECONDS`` limits through ``botocore.config.Config``.
- ``AwsMultiClient`` exposes exactly the seven client methods SWS calls: the
  six collector methods (``list_buckets``, ``get_bucket_location``,
  ``get_bucket_tagging``, ``list_functions``, ``list_tags``,
  ``get_cost_and_usage``) plus the single M11 read seam
  ``describe_instances``. No collector logic is duplicated here and no second
  abstraction layer exists beyond this thin adapter. There is deliberately no
  generic EC2 escape hatch (no ``**kwargs`` dispatch to arbitrary methods, no
  ``getattr``-style passthrough, no client handle exposure).
- The EC2 client (M11) is built lazily on first use, exactly like the boto3
  import: a workspace that never observes an instance never constructs an
  ``ec2`` client. This keeps ``describe_instances`` the only reason an EC2
  client can exist, and keeps read-only observation from adding a client to
  every inventory run.
- ``describe_instances`` returns the *paginator* for ``ec2:DescribeInstances``
  (AWS strongly recommends paginated requests). It is a read: SWS has no EC2
  write method here, so no mutation can be reached through this adapter.
- Lambda is a regional service: a single Lambda client targets one region.
  The factory creates the Lambda client for the configured region; region
  lists passed to ``collect_workspace`` are preserved as snapshot metadata,
  but Lambda inventory is scanned from the configured region only.
  Multi-region Lambda scanning requires per-region client injection and is
  deliberately outside M6.
- Cost Explorer (``ce``) uses its documented API region ``us-east-1``.
- No credentials are ever stored, logged, traced, or printed. The factory
  receives only configuration identities (an AWS region and/or profile
  name); boto3 resolves credentials from its standard chain at call time.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from .config import AWSConnectionConfig
from .constants import AWS_API_RETRY_ATTEMPTS, AWS_API_TIMEOUT_SECONDS

COST_EXPLORER_REGION: str = "us-east-1"
"""Cost Explorer's documented API region (the regional CE endpoint)."""

SWS_AWS_REGION_ENV: str = "SWS_AWS_REGION"
SWS_AWS_PROFILE_ENV: str = "SWS_AWS_PROFILE"
"""Explicit SWS-prefixed environment variables for opt-in real mode."""


def aws_config_from_env() -> AWSConnectionConfig | None:
    """Resolve an opt-in AWS connection config from SWS environment variables.

    Returns ``None`` when neither ``SWS_AWS_REGION`` nor ``SWS_AWS_PROFILE``
    is set (or both are blank), so importing, default startup, and the
    hermetic test suite remain fully credential-free. Blank values are
    treated as unset; the resulting config still fails fast through
    ``AWSConnectionConfig`` if it would be invalid.
    """
    region = (os.environ.get(SWS_AWS_REGION_ENV) or "").strip() or None
    profile = (os.environ.get(SWS_AWS_PROFILE_ENV) or "").strip() or None
    if region is None and profile is None:
        return None
    return AWSConnectionConfig(region=region, profile=profile)


class AwsMultiClient:
    """Combined client exposing the collector protocol methods.

    Adapts the three boto3 clients (s3 / lambda / ce) into the single client
    object that the existing collectors and ``DefaultSwsBackend`` consume.
    Each method forwards exactly as the collector calls it; kwargs pass
    through untouched so pagination and per-call parameters are preserved.

    M11 adds exactly one read-only EC2 method, ``describe_instances``. The
    ``ec2`` client is created on first use by ``ec2_client_factory`` so a
    caller that never observes an instance never builds one.

    M13-B adds ``client_for_region``. The regional collectors (Lambda, EC2)
    cannot invent a second region from the client they were handed: a boto3
    regional client answers for exactly one region, so reading a second region
    through it would return the *first* region's resources. ``client_for_region``
    resolves a client genuinely bound to the requested region and caches it, so
    a repeated request reuses one client rather than rebuilding it.

    This adds no AWS capability. It only lets SWS *address* the regions it was
    already asked about; the set of named operations is unchanged, and there is
    still no mutation method and no arbitrary-operation escape hatch.
    """

    def __init__(
        self,
        *,
        s3: Any,
        lambda_client: Any,
        cost_explorer: Any,
        ec2_client_factory: Callable[[], Any] | None = None,
        region: str | None = None,
        region_client_factory: Callable[[str], "AwsMultiClient"] | None = None,
    ) -> None:
        self._s3 = s3
        self._lambda_client = lambda_client
        self._cost_explorer = cost_explorer
        self._ec2_client_factory = ec2_client_factory
        self._ec2: Any = None
        self._region = region
        self._region_client_factory = region_client_factory
        self._regional_clients: dict[str, "AwsMultiClient"] = {}

    @property
    def region(self) -> str | None:
        """The single region this client answers for, or None if unbound."""
        return self._region

    def client_for_region(self, region: str) -> "AwsMultiClient":
        """Return a client genuinely bound to ``region``.

        Raises ``RuntimeError`` when this client was built without a
        ``region_client_factory`` (every injected test double, and any caller
        that assembled ``AwsMultiClient`` by hand). Failing loudly is the
        point: silently returning ``self`` would let a regional collector read
        one region and label the results with another, which is precisely the
        misattribution M13-B exists to eliminate. Collectors detect the absence
        of this method and handle the resulting coverage gap explicitly.
        """
        if self._region_client_factory is None:
            raise RuntimeError(
                "this client cannot address regions: it was built without a "
                "region_client_factory"
            )
        cached = self._regional_clients.get(region)
        if cached is None:
            cached = self._region_client_factory(region)
            self._regional_clients[region] = cached
        return cached

    def list_buckets(self, **kwargs: Any) -> Any:
        return self._s3.list_buckets(**kwargs)

    def get_bucket_location(self, **kwargs: Any) -> Any:
        return self._s3.get_bucket_location(**kwargs)

    def get_bucket_tagging(self, **kwargs: Any) -> Any:
        return self._s3.get_bucket_tagging(**kwargs)

    def list_functions(self, **kwargs: Any) -> Any:
        return self._lambda_client.list_functions(**kwargs)

    def list_tags(self, **kwargs: Any) -> Any:
        return self._lambda_client.list_tags(**kwargs)

    def get_cost_and_usage(self, **kwargs: Any) -> Any:
        return self._cost_explorer.get_cost_and_usage(**kwargs)

    def describe_instances(self) -> Any:
        """Return the paginator for the read-only ``ec2:DescribeInstances``.

        This is the whole M11 AWS surface: one named read operation, exposed
        as a paginator because AWS strongly recommends paginated requests.
        The provider drains the pages itself.

        There is intentionally no ``ec2_method(name, **kwargs)`` counterpart,
        so no caller can name an arbitrary EC2 operation -- and in particular
        no mutation (``ec2:StopInstances``) is reachable through SWS.
        """
        client = self._ec2
        if client is None:
            if self._ec2_client_factory is None:
                raise RuntimeError(
                    "no EC2 client factory is configured for this client"
                )
            client = self._ec2_client_factory()
            self._ec2 = client
        return client.get_paginator("describe_instances")


class AwsClientFactory:
    """Callable client factory that builds an ``AwsMultiClient`` per call.

    Construction never imports boto3 and never performs an AWS call; boto3,
    botocore, and the s3/lambda/ce clients are created inside ``__call__``, so
    real AWS plumbing happens only when a collector tool actually runs. The
    ``ec2`` client is created even later -- on the first ``describe_instances``
    call -- so no EC2 client exists unless an instance is observed.
    Caller-supplied
    ``session_factory`` (receiving the profile name) and
    ``client_config_factory`` (receiving the retry/timeout limits) keep the
    factory fully deterministic and credential-free for hermetic tests.
    """

    def __init__(
        self,
        config: AWSConnectionConfig,
        *,
        retry_attempts: int = AWS_API_RETRY_ATTEMPTS,
        timeout_seconds: int = AWS_API_TIMEOUT_SECONDS,
        session_factory: Callable[[str | None], Any] | None = None,
        client_config_factory: Callable[[int, int], Any] | None = None,
    ) -> None:
        self._config = config
        self._retry_attempts = retry_attempts
        self._timeout_seconds = timeout_seconds
        self._session_factory = session_factory
        self._client_config_factory = client_config_factory

    @staticmethod
    def _default_session_factory(profile_name: str | None) -> Any:
        import boto3  # lazy: boto3 stays optional

        return boto3.Session(profile_name=profile_name)

    @staticmethod
    def _botocore_config(retry_attempts: int, timeout_seconds: int) -> Any:
        from botocore.config import Config  # lazy: botocore stays optional

        return Config(
            retries={"max_attempts": retry_attempts, "mode": "standard"},
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
        )

    def __call__(self) -> AwsMultiClient:
        session_factory = self._session_factory or self._default_session_factory
        session = session_factory(self._config.profile)
        config_factory = (
            self._client_config_factory or self._botocore_config
        )
        client_config = config_factory(self._retry_attempts, self._timeout_seconds)
        region = self._config.region

        def ec2_client_factory() -> Any:
            # Lazy (M11): only an EC2 observation builds an ``ec2`` client, and
            # it inherits the same profile/region/retry/timeout plumbing.
            return session.client("ec2", region_name=region, config=client_config)

        # Account-global clients are built once and shared by every regional
        # client: ``list_buckets`` and Cost Explorer are not region-scoped, so
        # rebuilding them per region would only cost extra API setup.
        s3 = session.client("s3", region_name=region, config=client_config)
        cost_explorer = session.client(
            "ce", region_name=COST_EXPLORER_REGION, config=client_config
        )

        def region_client_factory(target_region: str) -> AwsMultiClient:
            # M13-B: build a client whose *regional* services actually answer
            # for ``target_region``. Constructing a client performs no AWS call,
            # so this stays as lazy as the rest of the plumbing; the per-service
            # clients are themselves created only when first used.
            def regional_ec2_client_factory() -> Any:
                return session.client(
                    "ec2", region_name=target_region, config=client_config
                )

            return AwsMultiClient(
                s3=s3,
                lambda_client=session.client(
                    "lambda", region_name=target_region, config=client_config
                ),
                cost_explorer=cost_explorer,
                ec2_client_factory=regional_ec2_client_factory,
                region=target_region,
                region_client_factory=region_client_factory,
            )

        return AwsMultiClient(
            s3=s3,
            lambda_client=session.client(
                "lambda", region_name=region, config=client_config
            ),
            cost_explorer=cost_explorer,
            ec2_client_factory=ec2_client_factory,
            region=region,
            region_client_factory=region_client_factory,
        )
