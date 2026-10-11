#!/usr/bin/env python3
"""Approver-side detached-approval signer for the first live StopInstances run.

THIS SCRIPT IS APPROVER TOOLING. It is meant to be run by the human approver on
the approver's own workstation, never on the executor, never by an agent, and it
must never be called from SWS runtime code.

It reuses the repository's existing cryptography and data model verbatim:

  * ``sws_agent.approval_signature.generate_signer_keypair``  -- key generation
  * ``sws_agent.approval_signature.public_key_raw_bytes``     -- pinned public key
  * ``sws_agent.approval_signature.ApprovalFields``           -- the exact 12 fields
  * ``sws_agent.approval_signature.sign_approval``            -- the Ed25519 signature

No new crypto format is invented. The JSON files this script reads and writes are
pure transport: the *only* thing that carries authority is the Ed25519 signature
over ``canonical_encode(ApprovalFields)``, which the executor re-derives from the
stored ticket at the execution gate.

Security properties of this tool:

  * The private key is written ONLY to the ``--key-file`` path (by default under
    the approver's own home directory, never inside the repository).
  * The private key is NEVER printed, logged, uploaded, or emitted in any output
    artifact. Only the *public* key and the signature are emitted.
  * The signed artifact the executor ingests contains only public material plus
    the signature.

Private-key storage
-------------------
By default the private key is stored, unencrypted, at::

    %USERPROFILE%\\.sws-approver\\ed25519_approver_key.pem   (Windows)
    ~/.sws-approver/ed25519_approver_key.pem                 (POSIX)

This file is the approver's sole authority. It must never be committed to Git,
uploaded to AWS, copied to the executor, pasted into a chat or an agent, or
stored on a shared volume. Treat it as you would an SSH private key. This script
best-effort narrows the file's permissions to the current user and refuses to
write it anywhere inside the repository.

Usage (run from anywhere; the script locates the repo itself):

    # 1) generate the approver key once and print the pinned public key
    python scripts/approver/sign_live_stop_approval.py init

    # 2) (later) sign the executor's signing-request into an approval artifact
    python scripts/approver/sign_live_stop_approval.py sign \
        --request signing_request.json \
        --out signed_approval.json

    # convenience: init-if-missing and sign in one step
    python scripts/approver/sign_live_stop_approval.py run \
        --request signing_request.json --out signed_approval.json

The signing-request schema (emitted by the executor's live-stop runner) is::

    {
      "ticket_id":          "...",   # minted by the executor's approval store
      "resource_id":        "i-0a881305e5bc3c133",
      "action":             "stop_resource",
      "plan_id":            "...",
      "execution_intent_key": "...",  # digest bound to the durable reservation
      "executor_instance_id": "i-0c37dfef1dd3fcea7",
      "account_id":         "527557823928",
      "region":             "us-east-1",
      "nonce":              "...",   # executor-minted, durably recorded on grant
      "execution_deadline": "2026-10-10T12:30:00+00:00"  # optional; else now+TTL
    }
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sws_agent.approval_signature import (  # noqa: E402
    ApprovalFields,
    generate_signer_keypair,
    public_key_raw_bytes,
    sign_approval,
)

DEFAULT_KEY_FILE = Path.home() / ".sws-approver" / "ed25519_approver_key.pem"
DEFAULT_TTL_MINUTES = 30
ARTIFACT_VERSION = 1

_REQUEST_REQUIRED = (
    "ticket_id",
    "resource_id",
    "action",
    "executor_instance_id",
    "account_id",
    "region",
    "nonce",
)


class ApproverToolError(Exception):
    """A usage or environment problem, reported to the approver verbatim."""


def _load_serialization():
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

    return serialization


def _harden_permissions(path: Path) -> None:
    """Best-effort: restrict the key file to the current user only."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        import subprocess  # noqa: PLC0415

        user = os.environ.get("USERNAME")
        if not user:
            return
        for args in (
            ["icacls", str(path), "/inheritance:r"],
            ["icacls", str(path), "/grant:r", f"{user}:(R,W)"],
        ):
            try:
                subprocess.run(
                    args, check=False, capture_output=True, shell=False
                )
            except OSError:
                return


def _refuse_inside_repo(path: Path) -> None:
    try:
        resolved = path.resolve()
    except OSError:
        return
    repo = _REPO_ROOT.resolve()
    if resolved == repo or repo in resolved.parents:
        raise ApproverToolError(
            f"refusing to write approver key material inside the repository "
            f"({resolved}); choose a path under {Path.home()!r} or another "
            "location that is never committed"
        )


def _public_key_report(public_key: Any) -> dict[str, str]:
    raw = public_key_raw_bytes(public_key)
    return {
        "raw_hex": raw.hex(),
        "raw_base64": base64.b64encode(raw).decode("ascii"),
        "raw_len": str(len(raw)),
    }


def _save_private_key(private_key: Any, key_file: Path) -> None:
    _refuse_inside_repo(key_file)
    serialization = _load_serialization()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ApproverToolError(
            f"refusing to overwrite an existing approver key at {key_file}; "
            "a new key would silently invalidate every pinned public key"
        ) from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
    finally:
        _harden_permissions(key_file)
    del pem  # do not keep key bytes alive longer than necessary


def _load_private_key(key_file: Path) -> Any:
    if not key_file.exists():
        raise ApproverToolError(
            f"no approver private key at {key_file}; run 'init' first "
            "(or the key lives at a different path: pass --key-file)"
        )
    serialization = _load_serialization()
    pem = key_file.read_bytes()
    try:
        return serialization.load_pem_private_key(pem, password=None)
    except Exception as exc:  # noqa: BLE001 - reported to the approver
        raise ApproverToolError(
            f"could not load an Ed25519 private key from {key_file}: {exc}"
        ) from exc


def _cmd_init(key_file: Path, force: bool) -> int:
    if key_file.exists() and not force:
        raise ApproverToolError(
            f"{key_file} already exists; refusing to overwrite. Re-run with "
            "--force ONLY if you intend to rotate and re-pin the key."
        )
    signer_key_id, private_key = generate_signer_keypair()
    _save_private_key(private_key, key_file)
    report = _public_key_report(private_key.public_key())
    print("Approver key generated.")
    print(f"  private key : {key_file}  (NEVER upload, commit, or copy to the executor)")
    print(f"  signer_key_id: {signer_key_id}")
    print(f"  public raw hex   : {report['raw_hex']}")
    print(f"  public raw base64: {report['raw_base64']}")
    print()
    print("Pin the PUBLIC key above on the executor (raw 32 bytes). The private")
    print("key stays here, on this workstation, and is never shared.")
    return 0


def _cmd_public_key(key_file: Path) -> int:
    from sws_agent.approval_signature import _key_id  # noqa: PLC0415

    private_key = _load_private_key(key_file)
    report = _public_key_report(private_key.public_key())
    print(f"signer_key_id  : {_key_id(private_key.public_key())}")
    print(f"public raw hex : {report['raw_hex']}")
    print(f"public base64  : {report['raw_base64']}")
    return 0


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApproverToolError(
            f"signing request field {field!r} is not ISO-8601: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise ApproverToolError(
            f"signing request field {field!r} must be timezone-aware: {value!r}"
        )
    return parsed.astimezone(timezone.utc)


def _build_fields(
    request: dict[str, Any], signer_key_id: str, ttl_minutes: int, issued_at: str | None
) -> ApprovalFields:
    for name in _REQUEST_REQUIRED:
        value = request.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ApproverToolError(
                f"signing request is missing required field {name!r}"
            )

    if issued_at is not None:
        issued = _parse_timestamp(issued_at, "issued_at")
    else:
        issued = datetime.now(timezone.utc)

    deadline_raw = request.get("execution_deadline")
    if deadline_raw is not None:
        if not isinstance(deadline_raw, str):
            raise ApproverToolError("execution_deadline must be an ISO-8601 string")
        deadline = _parse_timestamp(deadline_raw, "execution_deadline")
    else:
        deadline = issued + timedelta(minutes=ttl_minutes)

    if deadline <= issued:
        raise ApproverToolError(
            "execution_deadline must be after issued_at; the signed window would "
            "already be closed"
        )

    return ApprovalFields(
        ticket_id=request["ticket_id"],
        resource_id=request["resource_id"],
        action=request["action"],
        plan_id=request.get("plan_id"),
        execution_intent_key=request.get("execution_intent_key"),
        execution_deadline=deadline,
        executor_instance_id=request["executor_instance_id"],
        account_id=request["account_id"],
        region=request["region"],
        nonce=request["nonce"],
        issued_at=issued,
        signer_key_id=signer_key_id,
    )


def _cmd_sign(
    key_file: Path, request_path: Path, out_path: Path, ttl_minutes: int, issued_at: str | None
) -> int:
    if not request_path.exists():
        raise ApproverToolError(f"signing request not found: {request_path}")
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ApproverToolError(
            f"signing request {request_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(request, dict):
        raise ApproverToolError("signing request must be a JSON object")

    private_key = _load_private_key(key_file)
    from sws_agent.approval_signature import _key_id  # noqa: PLC0415

    signer_key_id = _key_id(private_key.public_key())
    fields = _build_fields(request, signer_key_id, ttl_minutes, issued_at)
    signed = sign_approval(fields, private_key)

    artifact = {
        "artifact_version": ARTIFACT_VERSION,
        "signer_key_id": signer_key_id,
        "signature": signed.signature,
        "signer_public_key": _public_key_report(private_key.public_key()),
        "fields": fields.as_payload(),
    }
    _refuse_inside_repo(out_path)
    out_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")

    print("Signed approval artifact written.")
    print(f"  artifact      : {out_path}")
    print(f"  ticket_id     : {fields.ticket_id}")
    print(f"  resource_id   : {fields.resource_id}")
    print(f"  action        : {fields.action}")
    print(f"  executor      : {fields.executor_instance_id}")
    print(f"  account/region: {fields.account_id} / {fields.region}")
    print(f"  signer_key_id : {signer_key_id}")
    print(f"  issued_at     : {fields.issued_at.isoformat()}")
    print(f"  deadline      : {fields.execution_deadline.isoformat()}")
    print(f"  nonce         : {fields.nonce}")
    print()
    print("Transfer ONLY this artifact (public material + signature) to the")
    print("executor. The private key never leaves this workstation.")
    return 0


def _cmd_run(
    key_file: Path, request_path: Path, out_path: Path, ttl_minutes: int, issued_at: str | None
) -> int:
    if not key_file.exists():
        _cmd_init(key_file, force=False)
        print()
    return _cmd_sign(key_file, request_path, out_path, ttl_minutes, issued_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sign_live_stop_approval",
        description=(
            "Approver-side Ed25519 detached-approval signer for the SWS first "
            "live StopInstances run. Run only on the approver's own workstation."
        ),
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        default=DEFAULT_KEY_FILE,
        help=f"private key path (default: {DEFAULT_KEY_FILE})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="generate the approver keypair")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing key (rotation)")

    sub.add_parser("public-key", help="print the pinned public key and signer_key_id")

    p_sign = sub.add_parser("sign", help="sign a signing-request into an artifact")
    p_sign.add_argument("--request", type=Path, required=True)
    p_sign.add_argument("--out", type=Path, required=True)
    p_sign.add_argument("--ttl-minutes", type=int, default=DEFAULT_TTL_MINUTES)
    p_sign.add_argument("--issued-at", default=None, help="override issued_at (ISO-8601)")

    p_run = sub.add_parser("run", help="init-if-missing, then sign")
    p_run.add_argument("--request", type=Path, required=True)
    p_run.add_argument("--out", type=Path, required=True)
    p_run.add_argument("--ttl-minutes", type=int, default=DEFAULT_TTL_MINUTES)
    p_run.add_argument("--issued-at", default=None)

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return _cmd_init(args.key_file, args.force)
        if args.command == "public-key":
            return _cmd_public_key(args.key_file)
        if args.command == "sign":
            return _cmd_sign(
                args.key_file, args.request, args.out, args.ttl_minutes, args.issued_at
            )
        if args.command == "run":
            return _cmd_run(
                args.key_file, args.request, args.out, args.ttl_minutes, args.issued_at
            )
    except ApproverToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command {args.command}")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
