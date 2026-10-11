"""Fixed live-stop entry point for the first authorized ec2:StopInstances run.

Two subcommands, and nothing else:

* ``prepare`` -- mint a durable PENDING ticket and emit ``signing_request.json``.
  This step performs **no** AWS call and needs no credentials.
* ``execute`` -- ingest the approver's ``signed_approval.json``, grant the
  ticket, and cross the boundary exactly once.

The entry point is deliberately narrow: every environment fact is an explicit
argument so the SSM invocation document can pin them as fixed literals rather
than letting anything be discovered. It prints only public material.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sws_agent.ec2_observation import DEFAULT_PARTITION

from sws_live.live_stop import (
    DEFAULT_DEADLINE_MINUTES,
    LiveStopError,
    execute_live_stop,
    parse_signed_approval_artifact,
    prepare_live_stop,
)


def _parse_pins(raw_pins: list[str]) -> dict[str, bytes]:
    """Parse repeatable ``signer_key_id=<hex>`` arguments into a pin map."""
    pins: dict[str, bytes] = {}
    for raw in raw_pins or []:
        key_id, sep, hex_value = raw.partition("=")
        if not sep:
            raise LiveStopError(f"--pin must be 'signer_key_id=<hex>', got {raw!r}")
        key_id = key_id.strip()
        try:
            material = bytes.fromhex(hex_value.strip())
        except ValueError as exc:
            raise LiveStopError(f"--pin for {key_id!r} is not valid hex: {exc}") from exc
        if len(material) != 32:
            raise LiveStopError(
                f"--pin for {key_id!r} must be 32 raw bytes, got {len(material)}"
            )
        pins[key_id] = material
    if not pins:
        raise LiveStopError("at least one --pin is required to verify an approval")
    return pins


def _cmd_prepare(args: argparse.Namespace) -> int:
    request = prepare_live_stop(
        ledger_dir=args.ledger_dir,
        target_instance_arn=args.target_instance_arn,
        region=args.region,
        executor_instance_id=args.executor_instance_id,
        account_id=args.account_id,
        snapshot_id=args.snapshot_id,
        partition=args.partition,
        plan_id=args.plan_id,
        nonce=args.nonce,
        rationale=args.rationale,
        deadline_minutes=args.deadline_minutes,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Signing request written for the approver.")
    print(f"  request    : {out}")
    print(f"  ticket_id  : {request['ticket_id']}")
    print(f"  plan_id    : {request['plan_id']}")
    print(f"  snapshot   : {request['snapshot_id']}")
    print(f"  intent key : {request['execution_intent_key']}")
    print(f"  nonce      : {request['nonce']}")
    print(f"  deadline   : {request['execution_deadline']}")
    return 0


def _cmd_execute(args: argparse.Namespace) -> int:
    artifact_path = Path(args.artifact)
    if not artifact_path.exists():
        raise LiveStopError(f"signed approval artifact not found: {artifact_path}")
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LiveStopError(f"{artifact_path} is not valid JSON: {exc}") from exc

    signed = parse_signed_approval_artifact(artifact)
    result = execute_live_stop(
        ledger_dir=args.ledger_dir,
        target_instance_arn=args.target_instance_arn,
        region=args.region,
        executor_instance_id=args.executor_instance_id,
        account_id=args.account_id,
        snapshot_id=args.snapshot_id,
        signed_approval=signed,
        approval_signer_public_keys=_parse_pins(args.pin),
        operator_identities=frozenset(args.operator_identity or ()),
        partition=args.partition,
        arm_witness=not args.no_witness,
        audit=not args.no_audit,
        worker_id=args.worker_id,
        decided_by=args.decided_by,
    )
    rendered = result.model_dump_json(indent=2)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.outcome.value == "verified_success" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="live-stop",
        description=(
            "Executor-side live-stop entry point for the first authorized "
            "ec2:StopInstances run."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ledger-dir", required=True, help="durable ledger directory")
        p.add_argument("--target-instance-arn", required=True)
        p.add_argument("--region", required=True)
        p.add_argument("--executor-instance-id", required=True)
        p.add_argument("--account-id", required=True)
        p.add_argument("--snapshot-id", required=True)
        p.add_argument("--partition", default=DEFAULT_PARTITION)

    prepare = sub.add_parser(
        "prepare", help="mint a ticket and emit signing_request.json"
    )
    add_common(prepare)
    prepare.add_argument("--plan-id", default=None)
    prepare.add_argument("--nonce", default=None)
    prepare.add_argument("--rationale", default="")
    prepare.add_argument(
        "--deadline-minutes", type=int, default=DEFAULT_DEADLINE_MINUTES
    )
    prepare.add_argument("--out", default="signing_request.json")
    prepare.set_defaults(func=_cmd_prepare)

    execute = sub.add_parser(
        "execute", help="ingest a signed artifact and cross the boundary once"
    )
    add_common(execute)
    execute.add_argument("--artifact", required=True, help="signed_approval.json")
    execute.add_argument(
        "--pin",
        action="append",
        default=[],
        metavar="KEY_ID=HEX",
        help="pinned approver public key (32 raw bytes, hex); repeatable",
    )
    execute.add_argument("--operator-identity", action="append", default=[])
    execute.add_argument("--worker-id", default=None)
    execute.add_argument("--decided-by", default="approver")
    execute.add_argument("--out", default=None)
    execute.add_argument("--no-witness", action="store_true")
    execute.add_argument("--no-audit", action="store_true")
    execute.set_defaults(func=_cmd_execute)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LiveStopError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
