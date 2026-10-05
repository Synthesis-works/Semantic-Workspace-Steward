"""Non-dispatching preflight evaluation for the first live stop.

This module decides whether a live ``StopInstances`` may proceed. It **never**
issues the mutation: there is no code path here that calls ``stop_instances``,
constructs a mutating client, or touches the network. That is a hard boundary,
not a style preference -- a preflight that could dispatch would be one more way
to dispatch, and the whole point of the milestone is that exactly one dispatch
happens, deliberately, after review.

What belongs here and what does not:

- Conditions verifiable **without** AWS access are evaluated mechanically:
  target scoping, IAM policy validity, retry configuration, witness attachment,
  and program invariants (no implemented actions, MCP surface unchanged).
- Conditions that require AWS access -- target state, approval freshness,
  ``DryRun`` outcome, credential provenance -- cannot be checked here and stay
  in ``docs/runbooks/first-live-stop-instances.md`` as human steps.

The distinction is explicit because a preflight gate that appears to cover
everything would let a reader believe the runbook's manual checks were automated.
They are not, and pretending otherwise would move risk rather than remove it.

Design rule for every check: **absence of evidence is a failure, never a pass.**
Each finding below records what was measured and how, so a reader can tell a
verified condition from an unchecked one.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Iterable

from sws_agent.ec2_mutation_client import (
    MutationClientSettings,
    effective_retries,
    validate_iam_policy,
)

__all__ = [
    "EXPECTED_MCP_TOOL_NAMES",
    "PreflightFinding",
    "PreflightReport",
    "PreflightRefusal",
    "evaluate_dispatch_evidence",
    "evaluate_preflight",
    "require_preflight",
]

#: The MCP tool surface this milestone was designed against. Pinning it here
#: rather than importing ``BUILTIN_TOOL_NAMES`` is deliberate: preflight must not
#: import the MCP stack, or the gate would drag a tool surface into a module whose
#: job is to check that surface. The cost is a duplicated constant, so drift is
#: caught by a test that asserts this equals the real
#: ``sws_agent.mcp.server.BUILTIN_TOOL_NAMES``.
EXPECTED_MCP_TOOL_NAMES: tuple[str, ...] = (
    "audit_workspace",
    "collect_workspace",
    "decide_ticket",
    "evaluate_workspace",
    "explain_resource",
    "get_cost_estimates",
    "get_relationships",
    "list_approvals",
    "request_approval",
)

#: One refusal reason per distinct defect, so a report reads as a list of things
#: to fix rather than a single opaque failure.
Check = str


@dataclasses.dataclass(frozen=True)
class PreflightFinding:
    """One checked condition, with the evidence that decided it."""

    check: Check
    passed: bool
    detail: str
    evidence: str

    def as_row(self) -> tuple[Check, str, str, str]:
        return (
            self.check,
            "pass" if self.passed else "FAIL",
            self.detail,
            self.evidence,
        )


@dataclasses.dataclass(frozen=True)
class PreflightReport:
    """The full set of findings. Authorization requires every one to pass."""

    findings: tuple[PreflightFinding, ...] = ()

    @property
    def failures(self) -> tuple[PreflightFinding, ...]:
        return tuple(f for f in self.findings if not f.passed)

    @property
    def authorized(self) -> bool:
        return not self.failures

    def reason(self) -> str:
        if self.authorized:
            return "all preflight checks passed"
        return "; ".join(f"{f.check}: {f.detail}" for f in self.failures)

    def rows(self) -> tuple[tuple[Check, str, str, str], ...]:
        return tuple(f.as_row() for f in self.findings)


class PreflightRefusal(RuntimeError):
    """Raised when a caller demands authorization the evidence does not support."""

    def __init__(self, report: PreflightReport) -> None:
        super().__init__(report.reason())
        self.report = report


def require_preflight(report: PreflightReport) -> None:
    """Raise unless the report authorizes dispatch."""
    if not report.authorized:
        raise PreflightRefusal(report)


# -- individual checks -------------------------------------------------------


def check_target_scoping(settings: MutationClientSettings, policy: dict[str, Any]) -> PreflightFinding:
    """The target is one instance, and the policy is scoped to that same instance.

    Two independent identifiers are involved -- an instance *id* used on the wire
    and an instance *ARN* used in IAM -- so they are compared rather than assumed
    to refer to the same thing. The id is also parsed back out of the ARN, which
    is what makes the comparison meaningful rather than two strings that happen
    to be present.
    """
    arn = settings.target_instance_arn
    match = re.search(r":instance/(i-[0-9a-f]+)$", arn)
    if match is None:
        return PreflightFinding(
            "target_scoping",
            False,
            f"target ARN is not an instance ARN: {arn!r}",
            f"settings.target_instance_arn={arn!r}",
        )
    arn_instance_id = match.group(1)
    if arn_instance_id != settings.instance_id:
        return PreflightFinding(
            "target_scoping",
            False,
            f"ARN identifies {arn_instance_id} but settings declare "
            f"{settings.instance_id}",
            f"arn={arn!r} settings.instance_id={settings.instance_id!r}",
        )
    try:
        validate_iam_policy(policy, settings)
    except Exception as exc:  # noqa: BLE001 - any validation failure is a refusal
        return PreflightFinding(
            "target_scoping",
            False,
            f"IAM policy is not valid for this target: {exc}",
            f"policy actions={_policy_actions(policy)}",
        )
    return PreflightFinding(
        "target_scoping",
        True,
        f"one instance, {arn_instance_id}, and the policy is scoped to its ARN",
        f"arn={arn!r}",
    )


def check_retry_configuration(client: Any) -> PreflightFinding:
    """The client in force resolves to exactly one attempt.

    Read from the client rather than from the settings, because a client
    reconfigured after construction would leave the settings intact while the
    effective behavior changed. This is the whole point of reading it here.
    """
    try:
        retries = effective_retries(client)
    except Exception as exc:  # noqa: BLE001
        return PreflightFinding(
            "retry_configuration",
            False,
            f"effective retry configuration could not be read: {exc}",
            "read failed",
        )
    total = retries.get("total_max_attempts")
    mode = retries.get("mode")
    evidence = f"effective_retries={retries!r}"
    if mode != "standard":
        return PreflightFinding(
            "retry_configuration",
            False,
            f"retry mode is {mode!r}, not 'standard'",
            evidence,
        )
    if total != 1:
        return PreflightFinding(
            "retry_configuration",
            False,
            f"effective total_max_attempts is {total!r}, not 1; botocore would "
            "permit a second dispatch",
            evidence,
        )
    return PreflightFinding(
        "retry_configuration", True, "exactly one attempt is permitted", evidence
    )


def check_witness_armed(witness: Any) -> PreflightFinding:
    """The witness is attached, and an unarmed one is treated as a failure.

    An armed flag that is false means the witness records nothing, and a witness
    that records nothing is indistinguishable from a run in which nothing was
    sent. Refusing before dispatch is the only point where that is still fixable.
    """
    armed = bool(getattr(witness, "armed", False))
    return PreflightFinding(
        "witness_armed",
        armed,
        "witness is attached to the client" if armed else "witness is not armed",
        f"armed={armed} records={len(getattr(witness, 'records', ()))}",
    )


def check_program_invariants(implemented: Iterable[str], tool_names: Iterable[str]) -> PreflightFinding:
    """The program is still in its pre-mutation state.

    Cheap, local, and worth asserting mechanically: if some earlier change
    flipped an action to ``implemented`` or added an MCP tool, the preflight
    conditions validated in earlier milestones no longer describe this program.

    Both halves are actually checked. This function used to accept ``tool_names``
    and ignore it while its own docstring claimed it would catch an added tool --
    the worst combination available, because the argument looked handled and the
    test suite passed the real nine every time, so nothing could surface the gap.
    """
    implemented_list = sorted(implemented)
    if implemented_list:
        return PreflightFinding(
            "program_invariants",
            False,
            f"actions marked implemented: {implemented_list}",
            f"implemented={implemented_list}",
        )
    observed = sorted(set(tool_names))
    # Set comparison, deliberately: ``observed`` is a sorted list and the pin is a
    # tuple, so ``!=`` on the two directly is always true. That bug made every
    # preflight refuse until a test with the real nine names caught it.
    if set(observed) != set(EXPECTED_MCP_TOOL_NAMES):
        added = sorted(set(observed) - set(EXPECTED_MCP_TOOL_NAMES))
        missing = sorted(set(EXPECTED_MCP_TOOL_NAMES) - set(observed))
        return PreflightFinding(
            "program_invariants",
            False,
            f"MCP tool surface is not the expected {len(EXPECTED_MCP_TOOL_NAMES)}"
            f" tools: added={added} missing={missing}",
            f"tools={observed}",
        )
    return PreflightFinding(
        "program_invariants",
        True,
        f"no action is implemented and the MCP surface is unchanged at "
        f"{len(EXPECTED_MCP_TOOL_NAMES)} tools; the mutation path is still inert",
        f"implemented=[] tools={observed}",
    )


def check_witnessed_targets(witness: Any, expected_instance_id: str) -> PreflightFinding:
    """Every witnessed request targeted exactly the pinned instance.

    Checked against the *witness record* rather than the handler's inputs,
    because the record is what the SDK was about to put on the wire. A handler
    that was handed the right target and a client that serialized a different
    one would be indistinguishable from each other otherwise.
    """
    records = tuple(getattr(witness, "records", ()))
    if not records:
        return PreflightFinding(
            "witnessed_targets",
            False,
            "no witnessed request to check; the witness must be armed and have "
            "observed the dispatch before this is meaningful",
            "records=0",
        )
    seen = [tuple(getattr(r, "instance_ids", ())) for r in records]
    wrong = [s for s in seen if s != (expected_instance_id,)]
    if wrong:
        return PreflightFinding(
            "witnessed_targets",
            False,
            f"witnessed targets {wrong} do not match the pinned "
            f"{expected_instance_id!r}",
            f"observed={seen!r}",
        )
    return PreflightFinding(
        "witnessed_targets",
        True,
        f"every witnessed request targeted {expected_instance_id}",
        f"observed={seen!r}",
    )


def check_dispatch_witness_clean(witness: Any) -> PreflightFinding:
    """The witness reports no violation of the single-dispatch invariant."""
    try:
        problems = tuple(witness.violations())
    except Exception as exc:  # noqa: BLE001
        return PreflightFinding(
            "single_dispatch",
            False,
            f"the witness could not be evaluated: {exc}",
            "violations() raised",
        )
    if problems:
        return PreflightFinding(
            "single_dispatch",
            False,
            f"{len(problems)} witness violation(s)",
            "; ".join(problems),
        )
    return PreflightFinding(
        "single_dispatch",
        True,
        "the witness observed exactly one dispatch",
        "violations=[]",
    )


def _policy_actions(policy: dict[str, Any]) -> list[str]:
    """Flatten action strings for evidence, tolerating missing statements."""
    actions: list[str] = []
    for statement in policy.get("Statement", []) or []:
        if not isinstance(statement, dict):
            continue
        value = statement.get("Action", [])
        if isinstance(value, str):
            actions.append(value)
        elif isinstance(value, list):
            actions.extend(str(v) for v in value)
    return sorted(actions)


# -- the gate -----------------------------------------------------------------


def evaluate_preflight(
    *,
    settings: MutationClientSettings,
    policy: dict[str, Any],
    client: Any,
    witness: Any,
    implemented_actions: Iterable[str],
    mcp_tool_names: Iterable[str],
) -> PreflightReport:
    """Everything checkable **before** dispatch.

    Returns a report rather than raising, so an operator sees the complete list
    of problems in one pass. Authorization is a separate, explicit step
    (:func:`require_preflight`), so a caller cannot mistake "evaluated" for
    "allowed".

    This performs no I/O. It reads in-memory state supplied by the caller.

    Pre-dispatch, the witness has observed nothing, so its *content* cannot be
    checked here -- only that it is attached. Content is checked afterwards by
    :func:`evaluate_dispatch_evidence`. Collapsing the two phases would force one
    of them to be wrong: a single function cannot both require a non-empty
    witness record and run before the record exists.
    """
    findings = (
        check_target_scoping(settings, policy),
        check_retry_configuration(client),
        check_witness_armed(witness),
        check_program_invariants(implemented_actions, mcp_tool_names),
        check_no_prior_observations(witness),
    )
    return PreflightReport(findings=findings)


def evaluate_dispatch_evidence(
    *,
    witness: Any,
    expected_instance_id: str,
) -> PreflightReport:
    """Everything checkable **after** dispatch, from the witness alone.

    This is the milestone's actual claim: that exactly one request reached the
    wire for the pinned target. It is deliberately separate from
    :func:`evaluate_preflight` so that "we checked before we went" and "we proved
    one dispatch" cannot be satisfied by the same call.

    Corroboration from outside the process -- the CloudTrail count -- is *not*
    checked here. No AWS access exists in this module, and a check that silently
    skipped it would present partial evidence as complete. It stays a runbook
    step for the same reason the rest of the runbook's AWS checks do.
    """
    findings = (
        check_witnessed_targets(witness, expected_instance_id),
        check_dispatch_witness_clean(witness),
    )
    return PreflightReport(findings=findings)


def check_no_prior_observations(witness: Any) -> PreflightFinding:
    """A witness being armed must not already hold records.

    A reused witness would carry a previous run's attempts into this one's
    count, and the sum would then be the total across both runs rather than the
    count for this dispatch. Refusing a non-empty witness keeps the single-run
    invariant true.
    """
    count = len(tuple(getattr(witness, "records", ())))
    return PreflightFinding(
        "witness_start_state",
        count == 0,
        "the witness has no prior observations"
        if count == 0
        else f"the witness already holds {count} record(s) and cannot certify this "
        "run; use a fresh witness",
        f"records={count}",
    )