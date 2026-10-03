"""Read-only AWS inventory mapping for S3 buckets, Lambda functions, and EC2
instances.

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

Scope (approved M2B, extended read-only by M13-A):
  - S3:  list_buckets, get_bucket_location, get_bucket_tagging.
         Versioning/encryption/policy/public-access enrichment is deferred.
  - Lambda: list_functions (paginated), list_tags.
         Environment variables are never collected.
  - EC2: describe_instances (paginated), read-only, opt-in per run.
         Owner tags are never collected (see ``Ec2InstanceCollector``).

M13-A opt-in rule. EC2 collection is opt-in (``collect_ec2=True``) rather than
part of the default run, for the same reason cost collection is opt-in: the
read is not always available. ``ec2:DescribeInstances`` is the only EC2
permission SWS uses, it is scoped to ``Resource: "*"``, and an account without
it would otherwise make every workspace snapshot permanently ``partial`` for a
service the caller did not ask about. The default run therefore still reports
exactly the types it attempted: ``resource_types`` never lists a type whose
collector was not invoked.
"""

from __future__ import annotations

from datetime import datetime
from itertools import chain
from typing import Any, Callable, Iterable, Iterator

from .constants import (
    MAX_RESOURCES_PER_INVENTORY_REQUEST,
    CollectionFailureCategory,
    SWSResourceType,
    TraceEventType,
)
from .ec2_observation import ec2_instance_facts
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


_EXHAUSTED: Any = object()
"""Sentinel distinguishing "the iterator ended" from a page that is ``None``."""


def _addressable_regions(client: Any, regions: list[str]) -> tuple[list[str], list[str]]:
    """Split ``regions`` into the ones this client can read and the ones it cannot.

    Returns ``(readable, unreadable)``.

    A regional boto3 client answers for exactly one region. A client exposing
    ``client_for_region`` (``aws.AwsMultiClient``) can genuinely address any
    region it is asked about, so every requested region is readable. Any other
    injected client -- every hand-built fake, the simulator -- is *region-blind*:
    it can report only whichever region it was built for.

    This distinction exists so a collector can never relabel one region's
    response as another's. When a caller asks for regions the client cannot
    address, the collector either refuses (EC2, which has no other way to learn
    an instance's region) or records the coverage gap explicitly (Lambda, which
    reads each instance's true region from its ARN).
    """
    if callable(getattr(client, "client_for_region", None)):
        return list(regions), []
    return regions[:1], list(regions[1:])


def _client_for_region(client: Any, region: str) -> Any:
    """Return the client bound to ``region``.

    Safe only when ``region`` came from ``_addressable_regions``' readable
    half: for a region-blind client that is the single region it was built for,
    so returning ``client`` unchanged is correct rather than a guess.
    """
    resolver = getattr(client, "client_for_region", None)
    if callable(resolver):
        return resolver(region)
    return client


def _collect_items(
    pages: Iterable[tuple[list[dict[str, Any]], bool]],
    map_item: Callable[[dict[str, Any]], ResourceRecord | None],
    *,
    remaining: int,
) -> tuple[list[ResourceRecord], bool]:
    """Drain ``pages`` into records, honoring ``remaining``.

    Returns ``(records, truncated)``. ``truncated`` is True **only** when this
    drain genuinely left data behind: either a page was broken mid-way with
    items not yet mapped, or the last page consumed reported that more pages
    follow. Both signals are supplied by the caller's page source, so neither
    is ever inferred from ``len(records) == remaining`` -- reaching the limit
    exactly at the end of the final page is not truncation.

    One implementation for every paginated collector, so the truncation
    contract cannot drift between them: the exactness of that flag is what
    ``WorkspaceSnapshot.truncated`` and the policy engine's confidence both
    rest on.
    """
    records: list[ResourceRecord] = []
    truncated = False
    for items, has_more in pages:
        for item in items:
            if len(records) >= remaining:
                truncated = True  # mid-page break with unconsumed items
                break
            record = map_item(item)
            if record is not None:
                records.append(record)
        if len(records) >= remaining:
            truncated = truncated or has_more
            break
    return records, truncated


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
        readable, unreadable = _addressable_regions(self._client, self._regions)
        records: list[ResourceRecord] = []
        truncated = False
        try:
            for region in readable:
                regional = _client_for_region(self._client, region)
                region_records, region_truncated = self._collect_region(
                    regional, limit - len(records)
                )
                records.extend(region_records)
                truncated = truncated or region_truncated
                if len(records) >= limit:
                    break
        except Exception as exc:  # primary list operation failure (fail-closed)
            self._trace_fail(f"{self._resource_type.value} inventory failed: {exc}")
            return []
        if unreadable:
            # M13-B: the records gathered so far are real and keep their true
            # regions (each was parsed from its own ARN), so they are preserved.
            # What is not established is whether the regions this client could
            # not address hold functions SWS has now missed, so the run reports
            # an incomplete collection of this type rather than a complete one.
            # The pre-M13-B behavior -- reading the same region once per
            # requested region -- duplicated every record and every tag lookup
            # while still proving nothing about the regions never read.
            self._trace_fail(
                f"lambda_function inventory covers only {readable[0]!r}; this "
                f"client cannot address {', '.join(unreadable)}",
                category=CollectionFailureCategory.PRIMARY,
            )
        self._trace_succeed(len(records), truncated=truncated)
        return records

    def _collect_region(
        self, regional_client: Any, remaining: int
    ) -> tuple[list[ResourceRecord], bool]:
        """Collect one region, returning ``(records, truncated)``.

        ``truncated`` is True exactly when this region genuinely had more
        functions than were returned: either a page was broken mid-way with
        unconsumed functions, or a ``NextMarker`` was present at the point
        the limit stopped collection. It is never inferred from
        ``count == limit`` alone (reaching the limit on the final page with
        no remaining data is not truncation).
        """
        return _collect_items(
            _lambda_pages(regional_client),
            # The Owner-tag lookup must go through the same regional client that
            # listed the function. ``list_tags`` is a regional Lambda operation,
            # so asking a client bound to another region would look up the tag of
            # an identically named function elsewhere -- or fail outright.
            lambda function: self._map_function(function, regional_client),
            remaining=remaining,
        )

    def _map_function(
        self, function: dict, regional_client: Any
    ) -> ResourceRecord | None:
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
            owner_tag=self._function_owner_tag(arn, regional_client),
            created_at=None,
            metrics={},
            raw=_function_raw(function),
        )

    def _function_owner_tag(self, arn: str, regional_client: Any) -> str | None:
        try:
            response = regional_client.list_tags(Resource=arn)
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


def _lambda_pages(client: Any) -> Iterator[tuple[list[dict[str, Any]], bool]]:
    """Yield ``(functions, has_more_pages)`` for each Lambda page.

    ``has_more_pages`` is the exact answer to "is there another page after
    this one", taken from the ``NextMarker`` Lambda itself reported. It is not
    inferred from the page being full.
    """
    marker: str | None = None
    while True:
        response = client.list_functions(**({"Marker": marker} if marker else {}))
        next_marker = response.get("NextMarker")
        yield response.get("Functions") or [], next_marker is not None
        if next_marker is None:
            return
        marker = next_marker


def _ec2_instance_pages(
    paginator: Any,
) -> Iterator[tuple[list[tuple[Any, Any]], bool]]:
    """Yield ``((instance, owner_id), ...)`` plus a has-more flag per page.

    The boto3 paginator reports "there is more" only by yielding it, so
    ``has_more_pages`` is established by pulling the next page and pushing it
    back. That is a real read of the iterator rather than an inference from a
    full page, which is what keeps ``truncated`` honest when the limit lands
    exactly on a page boundary.

    Each instance is paired with the ``OwnerId`` of the reservation enclosing
    it. That association is the only place the account identity exists:
    ``DescribeInstances`` puts ``OwnerId`` on the reservation, so flattening a
    page without carrying it along would silently discard the account and
    force ``account_id=None`` on every record.

    A malformed page raises, which the collector's primary-failure handler
    turns into an honest failed collection: an unreadable response is never
    reported as an empty one.
    """
    iterator = iter(paginator.paginate())
    while True:
        page = next(iterator, _EXHAUSTED)
        if page is _EXHAUSTED:
            return
        if not isinstance(page, dict):
            raise ValueError(
                f"DescribeInstances returned a malformed page: {type(page).__name__}"
            )
        reservations = page.get("Reservations") or []
        instances: list[tuple[Any, Any]] = []
        for reservation in reservations:
            if not isinstance(reservation, dict):
                raise ValueError(
                    "DescribeInstances returned a malformed reservation: "
                    f"{type(reservation).__name__}"
                )
            owner_id = reservation.get("OwnerId")
            instances.extend(
                (instance, owner_id)
                for instance in (reservation.get("Instances") or [])
            )
        following = next(iterator, _EXHAUSTED)
        has_more = following is not _EXHAUSTED
        if has_more:
            iterator = chain((following,), iterator)
        yield instances, has_more


class Ec2InstanceCollector(_InventoryCollectorBase):
    """Collects a bounded inventory of EC2 instances across regions, read-only.

    Added by M13-A, and deliberately the smallest EC2 read surface that can
    put an instance into a workspace snapshot: one logical
    ``ec2:DescribeInstances`` operation per region, drained to completion
    through the injected paginator seam (``aws.AwsMultiClient``). No mutating
    EC2 operation is called, named, or reachable from here, and
    ``PotentialAction.STOP_RESOURCE`` still has no registered handler.

    Honesty notes specific to EC2:

    - Regions are read one at a time, each through a client genuinely bound to
      that region (``client_for_region``). A client that cannot address regions
      is accepted for a single-region request and *refused* for several: an EC2
      response carries no region of its own, so labelling one region's instances
      with another's name would fabricate the identity a future
      ``ec2:StopInstances`` call would be resolved from. Lambda, whose ARNs do
      carry a region, records the same gap as an incomplete collection instead.
    - ``state`` is ``State.Name`` passed through byte-for-byte, or ``None``
      when AWS reported no state. It is never lowercased, mapped through
      ``Ec2InstanceState``, or defaulted to a benign value -- an instance whose
      state could not be read must reach policy as a record that says so.
    - ``resource_id`` is the ``InstanceId`` AWS returned. An item with no
      usable ``InstanceId`` is unidentifiable, so it is skipped with a PARSE
      failure rather than recorded under a synthesized id.
    - ``account_id`` is the enclosing reservation's ``OwnerId``, an observed
      fact, recorded only when it is a well-formed 12-digit account. An absent
      or malformed ``OwnerId`` is not a collection failure: the instance is
      still perfectly identified by its id, so the record is kept and simply
      carries no account identity.
    - ``arn`` is left ``None``. ``DescribeInstances`` returns no ARN, and
      M11 already documents the construction rule for one
      (``ec2_observation.Ec2InstanceObservationProvider``). Constructing it a
      second time here would create a second definition of the same fact.
    - ``owner_tag`` is always ``None``, and that is a statement about SWS
      rather than about the resource: this collector has no tag lookup, so it
      cannot know whether an instance carries an ``Owner`` tag. No failure is
      traced, because nothing failed. ``constants.OWNER_TAG_COLLECTED_RESOURCE_TYPES``
      records this so the policy engine never reads the absence as a finding.
    - ``created_at`` is ``LaunchTime`` when AWS sent a timezone-aware
      ``datetime``. A naive or non-datetime ``LaunchTime`` leaves
      ``created_at`` ``None`` rather than becoming an ambiguous authoritative
      timestamp; the observed value is still preserved in ``raw``.
    """

    def __init__(
        self,
        client: Any,
        *,
        regions: list[str] | None = None,
        trace: TraceSink | None = None,
    ):
        super().__init__(SWSResourceType.EC2_INSTANCE, trace=trace)
        if not regions:
            raise ValueError("at least one AWS region is required to collect EC2 inventory")
        for region in regions:
            if not isinstance(region, str) or not region.strip():
                raise ValueError("EC2 inventory regions must be non-empty strings")
        self._client = client
        self._regions = list(regions)

    def collect(self, *, limit: int | None = None) -> list[ResourceRecord]:
        limit = normalize_limit(limit)
        self._trace_start(f"collecting {self._resource_type.value} inventory")
        readable, unreadable = _addressable_regions(self._client, self._regions)
        if unreadable:
            # Unlike Lambda, an EC2 instance's region cannot be recovered from
            # the response: ``DescribeInstances`` returns no ARN and no region
            # field, so the requested region is the only record of where a
            # returned instance lives. Reading a client bound to some other
            # region and labelling the result with the requested one would
            # fabricate instance locations -- and this record is what a future
            # ``StopInstances`` call would be resolved from. Refuse instead.
            raise ValueError(
                "EC2 inventory cannot cover regions "
                f"{', '.join(unreadable)}: the injected describe_instances() "
                "seam cannot address regions and an EC2 response carries no "
                "region of its own. Use a client exposing client_for_region."
            )
        records: list[ResourceRecord] = []
        truncated = False
        try:
            for region in readable:
                regional = _client_for_region(self._client, region)
                region_records, region_truncated = self._collect_region(
                    regional.describe_instances(),
                    region,
                    limit - len(records),
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

    def _collect_region(
        self, paginator: Any, region: str, remaining: int
    ) -> tuple[list[ResourceRecord], bool]:
        return _collect_items(
            _ec2_instance_pages(paginator),
            lambda item: self._map_instance(item, region),
            remaining=remaining,
        )

    def _map_instance(
        self, item: tuple[Any, Any], region: str
    ) -> ResourceRecord | None:
        instance, owner_id = item
        if not isinstance(instance, dict):
            self._trace_fail(
                f"EC2 instance is malformed; skipped: {type(instance).__name__}",
                resource_id="unknown",
                category=CollectionFailureCategory.PARSE,
            )
            return None
        instance_id = instance.get("InstanceId")
        if not isinstance(instance_id, str) or not instance_id.strip():
            self._trace_fail(
                "EC2 instance has no InstanceId; skipped",
                resource_id="unknown",
                category=CollectionFailureCategory.PARSE,
            )
            return None
        return ResourceRecord(
            resource_id=instance_id,
            resource_type=SWSResourceType.EC2_INSTANCE,
            name=instance_id,
            region=region,
            owner_tag=None,
            created_at=_aware_launch_time(instance.get("LaunchTime")),
            state=_instance_state(instance),
            metrics={},
            raw=_instance_raw(instance),
            account_id=_well_formed_account_id(owner_id),
        )


def _well_formed_account_id(owner_id: Any) -> str | None:
    """Return ``owner_id`` when it is a 12-digit AWS account id, else None.

    ``ResourceRecord.account_id`` is pattern-constrained to 12 digits, and a
    surprising value is better recorded as absent than raised: one odd
    reservation must not abort a whole workspace collection. No account is ever
    invented for a response that did not carry one.
    """
    if isinstance(owner_id, str) and len(owner_id) == 12 and owner_id.isdigit():
        return owner_id
    return None


def _instance_state(instance: dict[str, Any]) -> str | None:
    """Return ``State.Name`` exactly as AWS sent it, or None when absent.

    Never normalized, cased, trimmed, or substituted: the policy engine owns
    the mapping from a recorded state to an action, and a collector that
    pre-decided which state it was looking at would make that decision twice.
    """
    state = instance.get("State")
    if not isinstance(state, dict):
        return None
    name = state.get("Name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name


def _aware_launch_time(value: Any) -> datetime | None:
    """Return ``value`` when it is a timezone-aware datetime, else None.

    ``canonicalize_resources`` rejects naive timestamps rather than guessing a
    timezone. Filtering here keeps one malformed ``LaunchTime`` from aborting
    an entire workspace collection while still recording what AWS sent.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value
    return None


def _instance_raw(instance: dict[str, Any]) -> dict[str, Any]:
    """Approved, non-secret instance fields only.

    The optional attribute set comes from ``ec2_instance_facts``, the same
    list the M11 observation provider uses, so the two readers of a
    DescribeInstances response cannot surface different attributes for the
    same resource. ``state`` is not repeated here: it has its own field.
    """
    raw: dict[str, Any] = ec2_instance_facts(instance)
    state_code = instance.get("State", {})
    if isinstance(state_code, dict) and state_code.get("Code") is not None:
        raw["state_code"] = state_code["Code"]
    return raw


INVENTORY_COLLECTORS: dict[SWSResourceType, Callable[..., _InventoryCollectorBase]] = {
    SWSResourceType.S3_BUCKET: S3BucketCollector,
    SWSResourceType.LAMBDA_FUNCTION: LambdaFunctionCollector,
    SWSResourceType.EC2_INSTANCE: Ec2InstanceCollector,
}
"""Registered inventory collectors, keyed by canonical resource type.

Membership means "SWS implements inventory for this type", not "every run
reads it": ``EC2_INSTANCE`` is opt-in per run (``collect_ec2``) for the IAM
reason documented at the top of this module.
"""


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
    if resource_type in _REGION_SCOPED_RESOURCE_TYPES:
        collector = factory(client, regions=regions, trace=trace)
    else:
        collector = factory(client, trace=trace)
    return collector.collect(limit=limit)


_REGION_SCOPED_RESOURCE_TYPES: frozenset[SWSResourceType] = frozenset(
    {SWSResourceType.LAMBDA_FUNCTION, SWSResourceType.EC2_INSTANCE}
)
"""Types whose collector is regional and therefore requires an explicit
``regions`` binding at construction.

EC2 and Lambda are both regional services reached through a single injected
client, so neither may fall back to a default region or an implicit global
scan: a silently-scoped inventory is not an inventory.
"""


def collect_all(
    *,
    client: Any,
    limit: int | None = None,
    trace: TraceSink | None = None,
    regions: list[str] | None = None,
    collect_ec2: bool = False,
) -> dict[SWSResourceType, list[ResourceRecord]]:
    """Collect inventory for every requested resource type.

    Returns a dict keyed by resource type with stable ordering (S3, then
    Lambda, then EC2).

    ``collect_ec2`` is opt-in. EC2 is left out of the returned mapping entirely
    when it is False, rather than being included with an empty list, so a
    caller cannot mistake "not requested" for "the account has no instances".
    """
    requested = [SWSResourceType.S3_BUCKET, SWSResourceType.LAMBDA_FUNCTION]
    if collect_ec2:
        requested.append(SWSResourceType.EC2_INSTANCE)
    return {
        resource_type: collect_resource_type(
            resource_type,
            client=client,
            limit=limit,
            trace=trace,
            regions=regions,
        )
        for resource_type in requested
    }