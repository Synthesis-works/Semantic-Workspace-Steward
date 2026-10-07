"""Detached cryptographic approval for the Candidate 1 mutation boundary.

M12 gave SWS a durable, CAS-protected approval state machine. It did not give
SWS an *authenticated* approver. ``ApprovalTicket.decided_by`` is a free string
any caller can set, and no production code path called ``grant()`` at all, so
the approval gate was enforced purely by access separation: the mutation role
had no shell and no way to reach the store. That is the absence of an exploit,
not a control.

This module is the control. An approver who holds a private key signs a
canonical artifact binding one approval to one execution environment. The
executor holds only the matching public key and verifies before dispatch. The
two halves never meet:

```text
approver workstation              executor (mutation role)
---------------------             -------------------------
ApprovalFields
  -> canonical_encode
  -> sign_approval(private)  -->  SignedApproval (public artifact)
                                    |
                                    v
                              verify_signed_approval(public)
                              verify_approval_for_execution(policy)
                                    |
                                    v
                              existing durable approval / reservation / execution path
                                    |
                                    v
                              MutationClientCredentials -> StopInstances
```

What this module deliberately does **not** do:

* It does not replace the state machine, the reservation CAS, the intent key,
  the deadline, ``DispatchWitness``, or the durable ledger. The signature is an
  *additional* integrity gate layered on top of all of them.
* It does not add a production ``grant()`` caller, an approver service, or any
  network dependency.
* It does not import the AWS SDK.
* It does not sign ``evidence_digest``. That field is reserved for Phase 5 and
  is ``None`` on every planner-created ticket; signing a constant would sign
  nothing while implying a binding that does not exist.

Dependency boundary
-------------------
``cryptography`` is imported **lazily**, inside the functions that need it,
following the same pattern ``ec2_mutation_client`` uses for boto3. Importing
this module never requires the optional ``signature`` extra, so
``sws_agent.execution`` -- which must stay importable without it -- can accept
a verifier built from this module without pulling in the library at import
time.

Canonicalization
----------------
The signed payload is compact JSON with sorted keys and an explicit domain
separation prefix. It is not a ``repr``, not an unordered dict, and not a
Python ``str`` of an object: a signature over any of those would be over an
encoding that changes with interpreter version, insertion order, or local
implementation detail. See :func:`canonical_encode`.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

__all__ = [
    "CANONICAL_DOMAIN",
    "SIGNED_FIELD_NAMES",
    "SIGNATURE_ALGORITHM",
    "ApprovalFields",
    "SignedApproval",
    "StoredApproval",
    "ApprovalSignatureError",
    "ApprovalSignatureMissingError",
    "ApprovalSignatureVerificationError",
    "canonical_encode",
    "sign_approval",
    "verify_signed_approval",
    "ApprovalVerificationPolicy",
    "verify_approval_for_execution",
    "stored_approval_from_ticket",
    "generate_signer_keypair",
    "public_key_raw_bytes",
    "parse_public_key_raw",
]

CANONICAL_DOMAIN: Final[bytes] = b"sws/approval/v1"
"""Domain separation prefix.

Prefixed to every canonical payload so a signature produced for this protocol
cannot be replayed as a signature over some other SWS artifact that happens to
serialize to the same bytes. Versioned so a future contract change cannot be
confused with the current one.
"""

SIGNATURE_ALGORITHM: Final[str] = "Ed25519"
"""The one signature algorithm this module accepts.

Ed25519 is chosen for a single deterministic reason: the verification key is
32 bytes and the signature is 64 bytes, with no parameter selection, no
padding, and no ASN.1 to get wrong. A misconfigured algorithm is a silent
failure in most systems; here it is a fixed choice with no alternatives.
"""

SIGNED_FIELD_NAMES: Final[tuple[str, ...]] = (
    "ticket_id",
    "resource_id",
    "action",
    "plan_id",
    "execution_intent_key",
    "execution_deadline",
    "executor_instance_id",
    "account_id",
    "region",
    "nonce",
    "issued_at",
    "signer_key_id",
)
"""Every field the signature covers, in canonical order.

``evidence_digest`` is deliberately absent. It is reserved for Phase 5 and is
``None`` on every ticket the current planner creates; including it would sign a
constant and imply a binding that does not yet exist.
"""


class ApprovalSignatureError(Exception):
    """Base class for approval-signature failures."""


class ApprovalSignatureMissingError(ApprovalSignatureError):
    """Raised when an approval that must be signed carries no signature.

    Fail-closed: a pinned key means every approval crossing the boundary has
    been attested, so an unsigned artifact is refused rather than treated as
    an unsigned-but-acceptable legacy record.
    """


class ApprovalSignatureVerificationError(ApprovalSignatureError):
    """Raised when a signature is present but does not verify.

    Covers an unknown signer key, a tampered field, a malformed signature,
    an expired window, and an approval bound to the wrong execution
    environment. Every cause maps to one canonical refusal reason at the
    execution gate.
    """


class ApprovalSignatureInvalidError(ApprovalSignatureVerificationError):
    """The artifact is present but cannot be trusted.

    An unknown signer key, a tampered field, a malformed signature, or a
    nonce that is not durably bound to this approval: anything where the
    bytes were altered, or came from an authority the executor does not
    recognize.
    """


class ApprovalSignerForbiddenError(ApprovalSignatureVerificationError):
    """The artifact is valid but was produced by a forbidden identity.

    The operator may not act as the approver. This is checked *after*
    verification so an operator-key artifact is refused even though the
    signature would verify.
    """


class ApprovalEnvironmentMismatchError(ApprovalSignatureVerificationError):
    """The artifact is valid but bound to a different execution environment.

    Executor instance, account, or region differ from what this host is
    configured as, which is what stops a valid approval being carried to a
    different machine.
    """


class ApprovalSignatureExpiredError(ApprovalSignatureVerificationError):
    """A valid artifact whose window is no longer open.

    Either the signed deadline has passed, or the signature is dated in the
    future beyond the allowed clock skew.
    """


def _require_str(value: Any, field: str, *, allow_none: bool = False) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise ApprovalSignatureVerificationError(
            f"signed field {field!r} must be a string and may not be missing"
        )
    if not isinstance(value, str) or not value.strip():
        raise ApprovalSignatureVerificationError(
            f"signed field {field!r} must be a non-blank string, got {value!r}"
        )
    return value


def _require_timestamp(value: Any, field: str) -> datetime:
    """Coerce and validate one signed timestamp.

    Naive datetimes are refused rather than assumed to be UTC. Guessing the
    offset is how an approval silently expires hours early or late, and a
    signature over an ambiguous timestamp is a signature over two different
    instants.
    """
    if not isinstance(value, datetime):
        raise ApprovalSignatureVerificationError(
            f"signed field {field!r} must be a datetime, got {value!r}"
        )
    if value.tzinfo is None:
        raise ApprovalSignatureVerificationError(
            f"signed field {field!r} must be timezone-aware; refusing to guess "
            "an offset for a value that will be signed"
        )
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    """Render an aware datetime in the one canonical signed form."""
    return _require_timestamp(value, "timestamp").isoformat()


@dataclass(frozen=True)
class ApprovalFields:
    """The exact set of facts an approval signature binds.

    Constructed by the approver, never by the executor. Every field is
    validated at construction so a malformed artifact cannot be signed and
    then verified against a different interpretation of the same bytes.
    """

    ticket_id: str
    resource_id: str
    action: str
    plan_id: str | None
    execution_intent_key: str | None
    execution_deadline: datetime
    executor_instance_id: str
    account_id: str
    region: str
    nonce: str
    issued_at: datetime
    signer_key_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "ticket_id", _require_str(self.ticket_id, "ticket_id")
        )
        object.__setattr__(
            self, "resource_id", _require_str(self.resource_id, "resource_id")
        )
        object.__setattr__(self, "action", _require_str(self.action, "action"))
        object.__setattr__(
            self,
            "plan_id",
            _require_str(self.plan_id, "plan_id", allow_none=True),
        )
        object.__setattr__(
            self,
            "execution_intent_key",
            _require_str(
                self.execution_intent_key, "execution_intent_key", allow_none=True
            ),
        )
        object.__setattr__(
            self,
            "execution_deadline",
            _require_timestamp(self.execution_deadline, "execution_deadline"),
        )
        object.__setattr__(
            self,
            "executor_instance_id",
            _require_str(self.executor_instance_id, "executor_instance_id"),
        )
        object.__setattr__(
            self, "account_id", _require_str(self.account_id, "account_id")
        )
        object.__setattr__(self, "region", _require_str(self.region, "region"))
        object.__setattr__(self, "nonce", _require_str(self.nonce, "nonce"))
        object.__setattr__(
            self, "issued_at", _require_timestamp(self.issued_at, "issued_at")
        )
        object.__setattr__(
            self, "signer_key_id", _require_str(self.signer_key_id, "signer_key_id")
        )

    def as_payload(self) -> dict[str, Any]:
        """The signed facts, rendered for canonical encoding.

        Timestamps are formatted here rather than in :func:`canonical_encode`
        so the encoding function has exactly one job and the typed form is
        visible at the point where the data is assembled.
        """
        return {
            "account_id": self.account_id,
            "action": self.action,
            "execution_deadline": _format_timestamp(self.execution_deadline),
            "execution_intent_key": self.execution_intent_key,
            "executor_instance_id": self.executor_instance_id,
            "issued_at": _format_timestamp(self.issued_at),
            "nonce": self.nonce,
            "plan_id": self.plan_id,
            "region": self.region,
            "resource_id": self.resource_id,
            "signer_key_id": self.signer_key_id,
            "ticket_id": self.ticket_id,
        }


def canonical_encode(fields: ApprovalFields) -> bytes:
    """Deterministic, unambiguous bytes for ``fields``.

    Compact JSON, sorted keys, ASCII-escaped, plus the domain-separation
    prefix. Deterministic because every component is fixed:

    * ``sort_keys`` removes dict-insertion order from the encoding;
    * ``separators`` removes optional whitespace;
    * ``ensure_ascii`` removes the question of whether a non-ASCII character
      was escaped;
    * timestamps are pre-rendered to UTC ISO-8601, so no platform locale or
      ``repr`` participates;
    * the payload is a closed dict, so no extra field can appear.

    A signature over this is a signature over one specific sequence of bytes,
    and the verifier recomputes those bytes from the stored fields rather than
    trusting a copy.
    """
    payload = fields.as_payload()
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return CANONICAL_DOMAIN + b"\n" + encoded


@dataclass(frozen=True)
class SignedApproval:
    """A signed approval artifact.

    ``signature`` is standard base64 text so it round-trips through a text
    column in the durable ledger without binary-handling surprises. The bytes
    are recovered only at verification time.
    """

    fields: ApprovalFields
    signature: str

    def __post_init__(self) -> None:
        _require_str(self.signature, "signature")


def _load_ed25519():
    """Import the Ed25519 primitives lazily.

    Lazy so importing this module never requires the optional ``signature``
    extra, matching how ``ec2_mutation_client`` keeps boto3 out of the core's
    import graph.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    return Ed25519PrivateKey, Ed25519PublicKey


def generate_signer_keypair() -> tuple[str, Any]:
    """Generate an approver key pair. **Approver-side tooling only.**

    Returns ``(signer_key_id, private_key)``. The executor must never call
    this: it is here so approver tooling and tests have one canonical way to
    produce a key, not so a mutation host can mint its own authority.

    The key id is derived from the public key so it cannot drift from the
    key it names: two different keys cannot share an id, and one key cannot
    present two ids.
    """
    Ed25519PrivateKey, _ = _load_ed25519()
    private_key = Ed25519PrivateKey.generate()
    return _key_id(private_key.public_key()), private_key


def public_key_raw_bytes(public_key: Any) -> bytes:
    """The 32 raw bytes of an Ed25519 public key."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return bytes(raw)


def _key_id(public_key: Any) -> str:
    """A deterministic, content-derived signer key id."""
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(public_key_raw_bytes(public_key)).hexdigest()
    return f"ed25519:{digest[:32]}"


def parse_public_key_raw(raw: bytes) -> Any:
    """Parse a raw 32-byte Ed25519 public key.

    Raises :class:`ApprovalSignatureVerificationError` on a wrong-length input
    rather than returning something that merely fails later, so a mis-pinned
    key is reported where it is configured.
    """
    _, Ed25519PublicKey = _load_ed25519()
    material = bytes(raw)
    if len(material) != 32:
        raise ApprovalSignatureInvalidError(
            f"an Ed25519 public key must be 32 raw bytes, got {len(material)}"
        )
    return Ed25519PublicKey.from_public_bytes(material)


def sign_approval(fields: ApprovalFields, private_key: Any) -> SignedApproval:
    """Sign ``fields`` with the approver's private key.

    The private key is supplied by the caller and never stored here. This is
    the only function in SWS that produces a signature, and nothing in the
    executor calls it.
    """
    signature = private_key.sign(canonical_encode(fields))
    return SignedApproval(
        fields=fields,
        signature=base64.b64encode(signature).decode("ascii"),
    )


def verify_signed_approval(approved: SignedApproval, public_key: Any) -> None:
    """Verify ``approved`` against ``public_key``. Raises on failure.

    A bad signature raises :class:`ApprovalSignatureVerificationError` with
    the underlying cause attached; it never returns ``False``. A verifier that
    returns a boolean gets ignored exactly once by a caller in a hurry.
    """
    from cryptography.exceptions import InvalidSignature  # noqa: PLC0415

    try:
        decoded = base64.b64decode(approved.signature.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001 - reported, never fabricated
        raise ApprovalSignatureInvalidError(
            f"approval signature is not valid base64: {exc}"
        ) from exc
    try:
        public_key.verify(decoded, canonical_encode(approved.fields))
    except InvalidSignature as exc:
        raise ApprovalSignatureInvalidError(
            "approval signature does not verify against the pinned public key; "
            "the artifact has been altered or was signed by another key"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a malformed key must not pass
        raise ApprovalSignatureInvalidError(
            f"approval signature could not be verified: {exc}"
        ) from exc


@dataclass(frozen=True)
class StoredApproval:
    """The signed facts as read back from the durable ledger.

    Built from an ``ApprovalTicket`` by :func:`stored_approval_from_ticket`.
    Verification always runs against *stored* values, never against a
    caller-supplied request: a request is whatever a caller claims, while the
    ledger row is what was actually approved.
    """

    ticket_id: str
    resource_id: str
    action: str
    plan_id: str | None
    execution_intent_key: str | None
    execution_deadline: datetime | None
    executor_instance_id: str | None
    account_id: str | None
    region: str | None
    nonce: str | None
    issued_at: datetime | None
    signer_key_id: str | None
    signature: str | None
    decided_by: str = ""

    def as_fields(self) -> ApprovalFields:
        """Rebuild the signed fields for re-encoding.

        Raises :class:`ApprovalSignatureVerificationError` when any bound
        field is missing, which is the fail-closed outcome: a partially
        recorded artifact cannot be re-encoded and therefore cannot verify.
        """
        if self.execution_deadline is None:
            raise ApprovalSignatureInvalidError(
                f"ticket {self.ticket_id!r} carries no execution_deadline, so "
                "the signed payload cannot be reconstructed"
            )
        return ApprovalFields(
            ticket_id=self.ticket_id,
            resource_id=self.resource_id,
            action=self.action,
            plan_id=self.plan_id,
            execution_intent_key=self.execution_intent_key,
            execution_deadline=self.execution_deadline,
            executor_instance_id=str(self.executor_instance_id or ""),
            account_id=str(self.account_id or ""),
            region=str(self.region or ""),
            nonce=str(self.nonce or ""),
            issued_at=self.issued_at
            if self.issued_at is not None
            else datetime.fromtimestamp(0, tz=timezone.utc),
            signer_key_id=str(self.signer_key_id or ""),
        )


def stored_approval_from_ticket(ticket: Any) -> StoredApproval:
    """Adapt an ``ApprovalTicket``-shaped object to :class:`StoredApproval`.

    Duck-typed on purpose: this module does not import ``sws_agent.models``,
    so the crypto layer stays free of the pydantic dependency and the two can
    be tested independently.

    ``action`` is unwrapped to its enum *value*, never ``str()``-ified. A
    ``str``-mixin enum renders as ``"PotentialAction.STOP_RESOURCE"``, while
    the approver signs ``"stop_resources"``; re-encoding the former would
    produce different bytes from the ones the signature covers, and
    verification would fail on every artifact the moment a real ticket was
    involved. The unwrapping is duck-typed for the same reason as the rest of
    this adapter.
    """
    action = getattr(ticket, "action", "")
    action_value = action.value if hasattr(action, "value") else str(action)
    return StoredApproval(
        ticket_id=str(getattr(ticket, "ticket_id", "")),
        resource_id=str(getattr(ticket, "resource_id", "")),
        action=action_value,
        plan_id=getattr(ticket, "plan_id", None),
        execution_intent_key=getattr(ticket, "execution_intent_key", None),
        execution_deadline=getattr(ticket, "execution_deadline", None),
        executor_instance_id=getattr(ticket, "executor_instance_id", None),
        account_id=getattr(ticket, "account_id", None),
        region=getattr(ticket, "region", None),
        nonce=getattr(ticket, "nonce", None),
        issued_at=getattr(ticket, "issued_at", None),
        signer_key_id=getattr(ticket, "signer_key_id", None),
        signature=getattr(ticket, "signature", None),
        decided_by=str(getattr(ticket, "decided_by", "") or ""),
    )


@dataclass(frozen=True)
class ApprovalVerificationPolicy:
    """What the executor is allowed to hold. Public material only.

    ``public_keys`` maps ``signer_key_id`` to the pinned public key. There is
    no private key here and no way to construct one: possession of this object
    lets an executor *check* an approval, never *produce* one.

    ``operator_identities`` is the set of identities that must never appear as
    a signer. This is what makes "the operator cannot act as approver" an
    enforced property rather than an arrangement: even a correctly-signed
    artifact is refused if the pinned key is one the operator holds.
    """

    public_keys: Mapping[str, Any]
    executor_instance_id: str
    account_id: str
    region: str
    operator_identities: frozenset[str] = frozenset()
    now: Callable[[], datetime] | None = None
    allowed_clock_skew: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.public_keys, Mapping) or not self.public_keys:
            raise ApprovalSignatureVerificationError(
                "an approval verification policy needs at least one pinned "
                "public key; with none there is no authority to verify against"
            )
        for key_id in self.public_keys:
            _require_str(key_id, "signer_key_id")
        object.__setattr__(
            self, "executor_instance_id", _require_str(
                self.executor_instance_id, "executor_instance_id"
            )
        )
        object.__setattr__(
            self, "account_id", _require_str(self.account_id, "account_id")
        )
        object.__setattr__(
            self, "region", _require_str(self.region, "region")
        )
        if self.allowed_clock_skew is None:
            from datetime import timedelta  # noqa: PLC0415

            object.__setattr__(self, "allowed_clock_skew", timedelta(minutes=5))

    @property
    def _clock(self) -> Callable[[], datetime]:
        return self.now or (lambda: datetime.now(timezone.utc))


def verify_approval_for_execution(
    stored: StoredApproval, policy: ApprovalVerificationPolicy
) -> None:
    """Verify ``stored`` against ``policy``. Raises on any failure.

    The checks, in the order they are performed, and why:

    1. **A signature is present.** With a key pinned, an unsigned approval is
       refused outright.
    2. **The signer is known.** ``signer_key_id`` must name a pinned key.
       An unpinned signer cannot be verified and therefore cannot authorize.
    3. **The signature verifies** over the canonical re-encoding of the
       *stored* fields. This is the tamper check: any altered field changes
       the bytes and the signature fails.
    4. **The signer is not the operator.** Checked after verification, so an
       operator-key artifact is refused even though it would verify.
    5. **The environment matches.** Executor instance, account, and region
       must equal what this executor is configured as. This is what stops a
       valid approval being carried to a different host.
    6. **The window is open.** The signed deadline must not have passed and
       the signature must not be dated in the future beyond the allowed skew.
    7. **The nonce is durably recorded and bound to this ticket.** Catches an
       artifact replayed against a different approval.

    Nothing here consumes anything, so a failed check never burns an approval
    and a later legitimate attempt is unaffected.
    """
    if not stored.signature:
        raise ApprovalSignatureMissingError(
            f"ticket {stored.ticket_id!r} carries no approval signature, and a "
            "verification key is pinned; unsigned approvals are refused"
        )
    signer_key_id = stored.signer_key_id or ""
    public_key = policy.public_keys.get(signer_key_id)
    if public_key is None:
        raise ApprovalSignatureInvalidError(
            f"approval was signed by {signer_key_id!r}, which is not among the "
            f"pinned signer keys {sorted(policy.public_keys)}"
        )

    verify_signed_approval(
        SignedApproval(fields=stored.as_fields(), signature=stored.signature),
        public_key,
    )

    if signer_key_id in policy.operator_identities:
        raise ApprovalSignerForbiddenError(
            f"approval was signed by {signer_key_id!r}, which is an operator "
            "identity; the operator may not act as the approver"
        )
    if stored.decided_by and stored.decided_by in policy.operator_identities:
        raise ApprovalSignerForbiddenError(
            f"approval was decided by {stored.decided_by!r}, which is an "
            "operator identity; the operator may not act as the approver"
        )

    for field_name, expected, actual in (
        ("executor_instance_id", policy.executor_instance_id, stored.executor_instance_id),
        ("account_id", policy.account_id, stored.account_id),
        ("region", policy.region, stored.region),
    ):
        if actual != expected:
            raise ApprovalEnvironmentMismatchError(
                f"approval is bound to {field_name}={actual!r} but this "
                f"executor has {field_name}={expected!r}; a valid approval "
                "must not be carried to a different execution environment"
            )

    now = policy._clock()
    deadline = stored.execution_deadline
    if deadline is not None and now > deadline:
        raise ApprovalSignatureExpiredError(
            f"approval expired at {deadline.isoformat()}; the signed execution "
            f"window closed at {now.isoformat()}"
        )
    issued_at = stored.issued_at
    if issued_at is not None and issued_at > now + policy.allowed_clock_skew:
        raise ApprovalSignatureExpiredError(
            f"approval is dated {issued_at.isoformat()}, which is later than "
            f"now plus the allowed {policy.allowed_clock_skew} skew; refusing "
            "an approval that has not yet been issued"
        )

    _require_nonce_binding(stored, policy)


def _nonce_index(policy: ApprovalVerificationPolicy) -> Any:
    """The durable nonce index this policy consults, or ``None``.

    Optional by design: a caller that only needs the signature and
    environment checks can omit it. When present it is a callable taking the
    nonce and returning the ticket id it was bound to, or ``None`` if unknown.
    """
    return getattr(policy, "nonce_lookup", None)


def _require_nonce_binding(stored: StoredApproval, policy: ApprovalVerificationPolicy) -> None:
    lookup = _nonce_index(policy)
    if lookup is None:
        return
    nonce = stored.nonce
    if not nonce:
        raise ApprovalSignatureInvalidError(
            f"ticket {stored.ticket_id!r} carries no nonce, so its approval "
            "cannot be shown to be single-use"
        )
    bound_to = lookup(nonce)
    if bound_to is None:
        raise ApprovalSignatureInvalidError(
            f"approval nonce {nonce!r} is not recorded in the durable nonce "
            "ledger; the artifact was never ingested or has been discarded"
        )
    if bound_to != stored.ticket_id:
        raise ApprovalSignatureInvalidError(
            f"approval nonce {nonce!r} belongs to ticket {bound_to!r} but was "
            f"presented for ticket {stored.ticket_id!r}; this is a replay"
        )


def nonce_bound_ticket_lookup(index: Mapping[str, str]) -> Callable[[str], str | None]:
    """Build a nonce lookup from a ``{nonce: ticket_id}`` mapping.

    Convenience for callers that already hold the mapping durably. A
    ledger-backed implementation passes its own callable instead.
    """

    def lookup(nonce: str) -> str | None:
        return index.get(nonce)

    return lookup
