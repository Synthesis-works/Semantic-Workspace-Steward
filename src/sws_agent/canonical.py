"""Canonical identity and normalization for SWS resource records.

Fresh SWS module (no SMS reuse). Runs after the M2B collectors have produced
plain ``ResourceRecord`` instances and normalizes them into the canonical
workspace representation without changing the collectors or their output.

Identity rules (M2C-A):
- Lambda: ``arn`` is the observed FunctionArn (``resource_id`` is copied);
  ``account_id`` is extracted from a well-formed Lambda ARN and accepted only
  when it is exactly 12 digits.
- S3: ``arn`` is deterministically derived as ``arn:<partition>:s3:::<name>``.
  ``account_id`` is never derived from ``Owner.ID``: S3 returns the canonical
  user ID (an obfuscated identifier), not the 12-digit AWS account ID, so it
  stays ``None`` for S3 records.
- Unknown/unsupported resource types pass through unchanged; identity data is
  never fabricated for types without a documented derivation rule.

Timestamp rule: timezone-aware ``created_at`` values are normalized to UTC;
naive datetimes are rejected with ``ValueError`` because a naive timestamp is
ambiguous and must never become an authoritative fact.

Canonicalization is deterministic and idempotent: repeated application
produces identical output. Caller-owned records are never mutated; each output
record is a fresh copy per the repository's Pydantic conventions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .constants import SWSResourceType
from .models import ResourceRecord


def _lambda_account_id(function_arn: str) -> str | None:
    """Extract the 12-digit account ID from a Lambda ARN, or None.

    ARN layout: ``arn:partition:lambda:region:account:function:name``.
    Returns None for anything that does not match the layout or for
    non-12-digit accounts, so no fabricated identity is ever recorded.
    """
    parts = function_arn.split(":")
    if len(parts) < 5 or parts[0] != "arn" or parts[2] != "lambda":
        return None
    account = parts[4]
    if len(account) != 12 or not account.isdigit():
        return None
    return account


def _normalize_created_at(created_at: datetime | None) -> datetime | None:
    """Normalize a timezone-aware timestamp to UTC.

    Naive datetimes are rejected because their interpretation is ambiguous.
    """
    if created_at is None:
        return None
    if created_at.tzinfo is None:
        raise ValueError(
            "created_at must be timezone-aware; refusing to guess a timezone"
        )
    return created_at.astimezone(timezone.utc)


def _derive_identity(
    record: ResourceRecord, *, partition: str
) -> dict[str, str | None]:
    """Derive canonical arn/account_id fields for a single resource type."""
    if record.resource_type is SWSResourceType.LAMBDA_FUNCTION:
        return {
            "arn": record.resource_id,
            "account_id": _lambda_account_id(record.resource_id),
        }
    if record.resource_type is SWSResourceType.S3_BUCKET:
        return {
            "arn": f"arn:{partition}:s3:::{record.resource_id}",
            "account_id": None,
        }
    return {"arn": record.arn, "account_id": record.account_id}


def canonicalize_resources(
    records: Iterable[ResourceRecord],
    *,
    partition: str = "aws",
) -> list[ResourceRecord]:
    """Return a canonical, deduplicated, deterministically ordered list.

    ``partition`` prefixes S3 ARNs and defaults to ``aws``. It does not
    affect Lambda resources, whose ARNs are observed from the API.

    Duplicates are resolved by ``(resource_type, resource_id)`` keeping the
    first occurrence. Output is sorted by ``(resource_type.value,
    resource_id)``. Input records are never mutated.
    """
    if not partition or not partition.strip():
        raise ValueError("partition must be a non-empty string")

    seen: set[tuple[SWSResourceType, str]] = set()
    canonical: list[ResourceRecord] = []
    for record in records:
        key = (record.resource_type, record.resource_id)
        if key in seen:
            continue
        seen.add(key)
        identity = _derive_identity(record, partition=partition)
        canonical.append(
            record.model_copy(
                update={
                    "arn": identity["arn"],
                    "account_id": identity["account_id"],
                    "created_at": _normalize_created_at(record.created_at),
                }
            )
        )

    return sorted(canonical, key=lambda r: (r.resource_type.value, r.resource_id))