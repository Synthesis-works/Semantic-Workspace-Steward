"""Read-only AWS inventory mapping for S3 buckets and Lambda functions.

Fresh SWS module (no SMS reuse). Collectors implement the existing
InventoryCollector protocol from interfaces.py but accept a constructor-
injected AWS client so the entire layer is hermetic and testable with
in-memory fakes; no boto3 dependency and no live AWS calls in this
milestone.

Honesty contract (matches trace.py): collectors record what the API
actually returned. They never fabricate resources, timestamps, metrics,
or enrichment results, and they never claim enrichment succeeded when the
call failed.

Trace metadata (M2C-B): every FAILED INVENTORY_QUERY event carries an
explicit ``category`` (``primary`` / ``enrichment`` / ``parse``), and a
SUCCEEDED event carries ``truncated: True`` only when collection genuinely
encountered more data than it returned. ``truncated`` is never inferred
from ``count == limit`` alone.

Scope (approved M2B):
  - S3:  list_buckets, get_bucket_location, get_bucket_tagging.
         Versioning/encryption/policy/public-access enrichment is deferred.
  - Lambda: list_functions (paginated), list_tags.
         Environment variables are never collected.
"""

from __future__ import annotations

from typing import Any, Callable

from .constants import (
    MAX_RESOURCES_PER_INVENTORY_REQUEST,
    CollectionFailureCategory,
    SWSResourceType,
    TraceEventType,
)
from .interfaces import TraceSink
from .models import ResourceRecord


class UnsupportedResourceTypeError(ValueError):
    """Raised when dispatch requests a resource type without a collector."""


def normalize_limit(limit: int | None) -> int:
    """Bound a caller-provided limit to the canonical inventory cap.

    ``None`` means the canonical default. Zero, negative, and non-integer
    values are rejected (matching the ``ge=1`` convention in config.py).
    This is the public form of the collector's limit normalization; the
    workspace builder (workspace.py) uses it so the effective per-type
    limit is stored on the snapshot.
    """
    if limit is None:
        return MAX_RESOURCES_PER_INVENTORY_REQUEST
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer or None")
    return limit


def _region_from_arn(function_arn: str) -> str | None:
    """Extract the region from a canonical Lambda ARN.

    ARN layout: ``arn:partition:lambda:region:account:function:name``.
    Returns None for anything that does not match, so no fabricated region
    is ever recorded.
    """
    parts = function_arn.split(":")
    if len(parts) >= 4 and parts[0] == "arn" and parts[2] == "lambda":
        return parts[3]
    return None


def _aws_error_code(exc: Exception) -> str | None:
    """Best-effort AWS error code from an SDK-shaped exception.

    botocore ``ClientError`` carries the code on ``exc.response["Error"]
    ["Code"]``. Reading it by attribute keeps SWS decoupled from boto3: the
    collector never imports an AWS SDK, and hermetic fakes reproducing the
    same attribute shape behave identically. Returns None when the exception
    carries no recognizable AWS error code.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str) and code:
                return code
    return None


class _InventoryCollectorBase:
    """Shared trace plumbing for inventory collectors."""

    def __init__(self, resource_type: SWSResourceType, *, trace: TraceSink | None):
        self._resource_type = resource_type
        self._trace = trace

    def _trace_start(self, message: str) -> None:
        if self._trace is not None:
            self._trace.record(
                TraceEventType.INVENTORY_QUERY,
                message,
                metadata={"resource_type": self._resource_type.value},
            )

    def _trace_succeed(self, count: int, *, truncated: bool = False) -> None:
        if self._trace is not None:
            metadata: dict[str, Any] = {
                "resource_type": self._resource_type.value,
                "count": count,
            }
            if truncated:
                metadata["truncated"] = True
            self._trace.succeed(
                TraceEventType.INVENTORY_QUERY,
                f"collected {count} {self._resource_type.value} resources",
                metadata=metadata,
            )

    def _trace_fail(
        self,
        message: str,
        *,
        resource_id: str | None = None,
        category: CollectionFailureCategory = CollectionFailureCategory.PRIMARY,
    ) -> None:
        if self._trace is not None:
            metadata: dict[str, Any] = {
                "resource_type": self._resource_type.value,
                "category": category.value,
            }
            if resource_id:
                metadata["resource_id"] = resource_id
            self._trace.fail(
                TraceEventType.INVENTORY_QUERY, message, metadata=metadata
            )


class S3BucketCollector(_InventoryCollectorBase):
    """Collects a bounded inventory of S3 buckets through an injected client.

    ``resource_id`` and ``name`` are the bucket name (globally unique).
    ``region`` comes from ``get_bucket_location``: ``None`` maps to
    ``us-east-1`` and the legacy ``EU`` maps to ``eu-west-1``. A failed
    location or tag lookup never discards the bucket record; the affected
    field becomes ``None`` and the failure is traced with the bucket name.
    """

    def __init__(self, client: Any, *, trace: TraceSink | None = None):
        super().__init__(SWSResourceType.S3_BUCKET, trace=trace)
        self._client = client

    def collect(self, *, limit: int | None = None) -> list[ResourceRecord]:
        limit = normalize_limit(limit)
        self._trace_start(f"collecting {self._resource_type.value} inventory")
        try:
            response = self._client.list_buckets()
        except Exception as exc:  # primary list operation failure
            self._trace_fail(f"{self._resource_type.value} inventory failed: {exc}")
            return []
        owner = response.get("Owner") or {}
        raw: dict[str, Any] = {}
        if owner.get("ID") is not None:
            raw["owner_id"] = owner["ID"]
        if owner.get("DisplayName") is not None:
            raw["owner_display_name"] = owner["DisplayName"]
        buckets = response.get("Buckets") or []
        records: list[ResourceRecord] = []
        for bucket in buckets:
            if len(records) >= limit:
                break
            name = bucket.get("Name")
            if not name:
                continue
            records.append(self._map_bucket(name, bucket, dict(raw)))
        truncated = len(records) == limit and len(buckets) > limit
        self._trace_succeed(len(records), truncated=truncated)
        return records

    def _map_bucket(self, name: str, bucket: dict, raw: dict[str, Any]) -> ResourceRecord:
        return ResourceRecord(
            resource_id=name,
            resource_type=SWSResourceType.S3_BUCKET,
            name=name,
            region=self._bucket_region(name),
            owner_tag=self._bucket_owner_tag(name),
            created_at=bucket.get("CreationDate"),
            metrics={},
            raw=raw,
        )

    def _bucket_region(self, name: str) -> str | None:
        try:
            response = self._client.get_bucket_location(Bucket=name)
        except Exception as exc:
            self._trace_fail(
                f"failed to read region for bucket '{name}': {exc}",
                resource_id=name,
                category=CollectionFailureCategory.ENRICHMENT,
            )
            return None
        location = response.get("LocationConstraint")
        if location is None:
            return "us-east-1"
        if location == "EU":
            return "eu-west-1"
        return location

    def _bucket_owner_tag(self, name: str) -> str | None:
        try:
            response = self._client.get_bucket_tagging(Bucket=name)
        except Exception as exc:
            if _aws_error_code(exc) == "NoSuchTagSet":
                # Real S3 raises NoSuchTagSet when a bucket simply has no
                # tags. That absence is a fact, not a collection failure: the
                # record is preserved with owner_tag=None and no ENRICHMENT
                # failure is traced, so a normal untagged bucket never makes
                # the workspace snapshot partial or downgrades policy
                # confidence.
                return None
            self._trace_fail(
                f"failed to read tags for bucket '{name}': {exc}",
                resource_id=name,
                category=CollectionFailureCategory.ENRICHMENT,
            )
            return None
        for tag in response.get("TagSet") or []:
            if tag.get("Key") == "Owner":
                return tag.get("Value")
        return None


class LambdaFunctionCollector(_InventoryCollectorBase):
    """Collects a bounded inventory of Lambda functions across regions.

    ``resource_id`` is the FunctionArn and ``region`` is parsed from that
    ARN. Lambda's API exposes ``LastModified`` (a change timestamp), not a
    creation time, so ``created_at`` is always None. ``raw`` is limited to
    approved, non-secret configuration fields; Environment variables are
    never collected.

    Region safety: region collection is driven by an explicit, non-empty
    ``regions`` list. No implicit default region or global scan occurs.
    """

    def __init__(
        self,
        client: Any,
        *,
        regions: list[str] | None = None,
        trace: TraceSink | None = None,
    ):
        super().__init__(SWSResourceType.LAMBDA_FUNCTION, trace=trace)
        if not regions:
            raise ValueError("at least one AWS region is required to collect Lambda inventory")
        for region in regions:
            if not isinstance(region, str) or not region.strip():
                raise ValueError("Lambda inventory regions must be non-empty strings")
        self._client = client
        self._regions = list(regions)

    def collect(self, *, limit: int | None = None) -> list[ResourceRecord]:
        limit = normalize_limit(limit)
        self._trace_start(f"collecting {self._resource_type.value} inventory")
        records: list[ResourceRecord] = []
        truncated = False
        try:
            for region in self._regions:
                region_records, region_truncated = self._collect_region(
                    limit - len(records)
                )
                records.extend(region_records)
                truncated = truncated or region_truncated
                if len(records) >= limit:
                    break
        except Exception as exc:  # primary list operation failure (fail-closed)
            self._trace_fail(f"{self._resource_type.value} inventory failed: {exc}")
            return []
        self._trace_succeed(len(records), truncated=truncated)
        return records

    def _collect_region(self, remaining: int) -> tuple[list[ResourceRecord], bool]:
        """Collect one region, returning ``(records, truncated)``.

        ``truncated`` is True exactly when this region genuinely had more
        functions than were returned: either a page was broken mid-way with
        unconsumed functions, or a ``NextMarker`` was present at the point
        the limit stopped collection. It is never inferred from
        ``count == limit`` alone (reaching the limit on the final page with
        no remaining data is not truncation).
        """
        records: list[ResourceRecord] = []
        marker: str | None = None
        truncated = False
        while True:
            response = self._client.list_functions(
                **({"Marker": marker} if marker else {})
            )
            functions = response.get("Functions") or []
            for function in functions:
                if len(records) >= remaining:
                    truncated = True  # mid-page break with unconsumed functions
                    break
                record = self._map_function(function)
                if record is not None:
                    records.append(record)
            marker = response.get("NextMarker")
            if marker is None or len(records) >= remaining:
                if len(records) >= remaining and marker is not None:
                    truncated = True  # stopped at the limit with more pages
                break
        return records, truncated

    def _map_function(self, function: dict) -> ResourceRecord | None:
        arn = function.get("FunctionArn")
        if not arn:
            self._trace_fail(
                f"Lambda function has no FunctionArn; skipped: "
                f"{function.get('FunctionName')}",
                resource_id=str(function.get("FunctionName") or "unknown"),
                category=CollectionFailureCategory.PARSE,
            )
            return None
        region = _region_from_arn(arn)
        if region is None:
            self._trace_fail(
                f"unparseable Lambda FunctionArn; skipped: {arn}",
                resource_id=arn,
                category=CollectionFailureCategory.PARSE,
            )
            return None
        return ResourceRecord(
            resource_id=arn,
            resource_type=SWSResourceType.LAMBDA_FUNCTION,
            name=function.get("FunctionName"),
            region=region,
            owner_tag=self._function_owner_tag(arn),
            created_at=None,
            metrics={},
            raw=_function_raw(function),
        )

    def _function_owner_tag(self, arn: str) -> str | None:
        try:
            response = self._client.list_tags(Target=arn)
        except Exception as exc:
            self._trace_fail(
                f"failed to read tags for Lambda function '{arn}': {exc}",
                resource_id=arn,
                category=CollectionFailureCategory.ENRICHMENT,
            )
            return None
        return (response.get("Tags") or {}).get("Owner")


def _function_raw(function: dict) -> dict[str, Any]:
    """Approved, non-secret configuration fields only.

    Environment variables and code payloads are deliberately excluded.
    """
    return {
        "runtime": function.get("Runtime"),
        "handler": function.get("Handler"),
        "memory_mb": function.get("MemorySize"),
        "timeout_seconds": function.get("Timeout"),
        "package_type": function.get("PackageType"),
        "architectures": function.get("Architectures") or [],
        "state": function.get("State"),
        "last_modified": function.get("LastModified"),
        "vpc_enabled": bool(function.get("VpcConfig")),
        "layered": bool(function.get("Layers")),
    }


INVENTORY_COLLECTORS: dict[SWSResourceType, Callable[..., _InventoryCollectorBase]] = {
    SWSResourceType.S3_BUCKET: S3BucketCollector,
    SWSResourceType.LAMBDA_FUNCTION: LambdaFunctionCollector,
}
"""Registered inventory collectors, keyed by canonical resource type."""


def collect_resource_type(
    resource_type: SWSResourceType,
    *,
    client: Any,
    limit: int | None = None,
    trace: TraceSink | None = None,
    regions: list[str] | None = None,
) -> list[ResourceRecord]:
    """Collect inventory for one supported resource type.

    Raises UnsupportedResourceTypeError for types without a collector.
    """
    factory = INVENTORY_COLLECTORS.get(resource_type)
    if factory is None:
        raise UnsupportedResourceTypeError(
            f"inventory is not implemented for resource type '{resource_type.value}'"
        )
    if resource_type is SWSResourceType.LAMBDA_FUNCTION:
        collector = factory(client, regions=regions, trace=trace)
    else:
        collector = factory(client, trace=trace)
    return collector.collect(limit=limit)


def collect_all(
    *,
    client: Any,
    limit: int | None = None,
    trace: TraceSink | None = None,
    regions: list[str] | None = None,
) -> dict[SWSResourceType, list[ResourceRecord]]:
    """Collect inventory for all supported resource types.

    Returns a dict keyed by resource type with stable ordering (S3 first,
    then Lambda).
    """
    return {
        resource_type: collect_resource_type(
            resource_type,
            client=client,
            limit=limit,
            trace=trace,
            regions=regions,
        )
        for resource_type in (
            SWSResourceType.S3_BUCKET,
            SWSResourceType.LAMBDA_FUNCTION,
        )
    }