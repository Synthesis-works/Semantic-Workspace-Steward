"""Deterministic synthetic demo dataset and backend for the M5 simulator.

All data here is CLEARLY synthetic: it is a plausible-looking workspace used
only to demonstrate the SWS workflow locally. It is never presented as live
AWS data. The demo backend implements the M4 ``SwsBackend`` seam and
delegates policy evaluation and relationship derivation to the REAL
deterministic SWS core, so the interactive demo exercises the real M4 MCP
tool boundary over synthetic inventory only.

Honesty rules applied here (mirroring ``docs/reuse-decisions.md`` and the
M5 requirements):
  - no fabricated savings, audit history, actions, or usage statistics;
  - every cost figure is ``CostEstimate(projected=True)`` and states that it
    is synthetic;
  - approvals only mutate the in-memory ticket store; nothing ever executes;
  - explanations are ``ClaimKind.INTERPRETED`` demo text that cannot change
    any deterministic policy decision.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from ..approval import InMemoryApprovalStore
from ..constants import ExecutionMode, PotentialAction, SWSResourceType
from ..models import (
    ActionPlan,
    ApprovalTicket,
    ClaimKind,
    CostCollectionReport,
    CostEstimate,
    ExplanationResult,
    PolicyDecision,
    ResourceRecord,
    ResourceRelationship,
    WorkspaceSnapshot,
)
from ..policy import evaluate_workspace as _evaluate_workspace
from ..relationships import derive_relationships as _derive_relationships
from ..workflow import ActionPlanner

# Sentinel distinguishing this workspace from any live-AWS snapshot.
DEMO_PROVIDER = "demo"
DEMO_ACCOUNT_ID = None  # raw collected inventory has no account_id yet
DEMO_REGIONS = ("us-east-1", "us-west-2", "eu-west-1")

_BUCKETS: tuple[tuple[str, str, str | None, int, int], ...] = (
    # (resource_id, region, owner_tag, size_bytes, object_count)
    ("web-assets-cdn", "us-east-1", "web", 1_200_000_000, 84),
    ("billing-exports", "us-east-1", "billing", 89_200_000, 7),
    ("data-lake-raw", "us-east-1", "data", 41_000_000_000, 1_240),
    ("data-lake-cleaned", "us-east-1", "data", 18_300_000_000, 920),
    ("ml-training-sets", "us-west-2", "ml", 6_500_000_000, 415),
    ("backups-app-a", "us-east-1", "platform", 900_000_000, 54),
    ("analytics-staging", "us-west-2", "analytics", 310_000_000, 22),
    ("logs-archive-2024", "us-east-1", "ops", 2_800_000_000, 390),
    ("infra-terraform-state", "us-east-1", "platform", 4_200_000, 9),
    ("ghost-bucket-no-owner", "us-east-1", None, 42_000_000, 0),
    ("images-media-processing", "eu-west-1", "media", 7_900_000_000, 4_120),
    ("platform-artifacts", "us-east-1", "platform", 610_000_000, 231),
)

_FUNCTIONS: tuple[tuple[str, str, str | None, int, int], ...] = (
    # (resource_id, region, owner_tag, invocations, avg_duration_ms)
    ("report-daily-generator", "us-east-1", "billing", 318_000, 1_420),
    ("image-resize-prod", "eu-west-1", "media", 2_400_000, 880),
    ("event-forwarder", "us-east-1", "platform", 1_150_000, 62),
    ("metrics-aggregator", "us-west-2", "ops", 96_000, 1_900),
    ("cache-warmer", "us-east-1", "web", 720_000, 45),
    ("orphan-lambda-no-owner", "us-east-1", None, 210, 2_100),
    ("csv-to-parquet", "us-east-1", "data", 44_000, 3_400),
    ("healthcheck-pinger", "eu-west-1", "platform", 12_000_000, 28),
)


def _demo_resources() -> list[ResourceRecord]:
    resources: list[ResourceRecord] = []
    for resource_id, region, owner, size, objects in _BUCKETS:
        resources.append(
            ResourceRecord(
                resource_id=resource_id,
                resource_type=SWSResourceType.S3_BUCKET,
                name=resource_id,
                region=region,
                owner_tag=owner,
                metrics={"size_bytes": float(size), "object_count": float(objects)},
            )
        )
    for resource_id, region, owner, invocations, duration in _FUNCTIONS:
        resources.append(
            ResourceRecord(
                resource_id=resource_id,
                resource_type=SWSResourceType.LAMBDA_FUNCTION,
                name=resource_id,
                region=region,
                owner_tag=owner,
                metrics={
                    "invocations": float(invocations),
                    "avg_duration_ms": float(duration),
                },
            )
        )
    resources.sort(key=lambda record: record.resource_id)
    return resources


def build_demo_snapshot() -> WorkspaceSnapshot:
    """The deterministic synthetic workspace used by the demo (20 resources)."""
    resources = _demo_resources()
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    return WorkspaceSnapshot(
        snapshot_id="demo-synthetic-workspace",
        created_at=now,
        collected_at=now,
        run_id="demo-run-0001",
        requested_limit=100,
        regions=list(DEMO_REGIONS),
        resource_types=[
            SWSResourceType.S3_BUCKET,
            SWSResourceType.LAMBDA_FUNCTION,
        ],
        resources=resources,
        counts={
            SWSResourceType.S3_BUCKET: len(_BUCKETS),
            SWSResourceType.LAMBDA_FUNCTION: len(_FUNCTIONS),
        },
        partial=False,
        truncated=False,
    )


def demo_cost_estimates() -> list[CostEstimate]:
    """Synthetic, clearly-projected cost figures for the demo workspace.

    There are deliberately no "savings" line items and no implied history.
    """
    return [
        CostEstimate(
            line_item="workspace total",
            amount_usd=241.37,
            basis="synthetic demo projection",
            projected=True,
            assumptions=["synthetic demo dataset; not a real AWS bill"],
        ),
        CostEstimate(
            line_item="data engineering group",
            amount_usd=96.40,
            basis="synthetic demo projection",
            projected=True,
            assumptions=["synthetic demo data; illustrative only"],
        ),
        CostEstimate(
            line_item="media processing group",
            amount_usd=58.12,
            basis="synthetic demo projection",
            projected=True,
            assumptions=["synthetic demo data; illustrative only"],
        ),
    ]


def demo_resource_references() -> list[dict[str, list[str]]]:
    """Keyword references so the rule-based router can resolve resources."""
    references: list[dict[str, list[str]]] = []
    for resource in _demo_resources():
        references.append(
            {
                "id": resource.resource_id,
                "keywords": [resource.resource_id, resource.resource_id.replace("-", " ")],
            }
        )
    return references


def _demo_explanation(
    resource: ResourceRecord, decision: PolicyDecision
) -> ExplanationResult:
    if decision.rule:
        text = (
            f"{resource.name} ({resource.resource_type.value}) is flagged by "
            f"deterministic policy rule '{decision.rule}': "
            f"{decision.rationale}"
        )
    else:
        text = (
            f"{resource.name} ({resource.resource_type.value}) triggers no "
            f"deterministic policy rule; the recommended action stays "
            f"{decision.recommended_action.value}."
        )
    return ExplanationResult(
        text=text,
        claim_kind=ClaimKind.INTERPRETED,
        provider=DEMO_PROVIDER,
        reason="synthetic demo explanation; cannot change deterministic policy",
    )


class DemoBackend:
    """SwsBackend for the demo: synthetic inventory, real SWS core logic.

    ``collect_workspace`` returns the deterministic synthetic snapshot;
    ``derive_relationships`` and ``evaluate_workspace`` delegate to the real
    SWS core so the demo genuinely exercises the deterministic engine.
    """

    def __init__(self) -> None:
        self.snapshot = build_demo_snapshot()
        self._approvals = InMemoryApprovalStore()
        self._planner = ActionPlanner(
            approval_store=self._approvals,
            execution_mode=ExecutionMode.SAFE,
        )
        self._seed_tickets()

    def _seed_tickets(self) -> None:
        for ticket_id, resource_id, rationale in (
            (
                "demo-ticket-0001",
                "ghost-bucket-no-owner",
                "Unattributed bucket with zero observed object access. "
                "Candidate for review; nothing executes in this demo.",
            ),
            (
                "demo-ticket-0002",
                "orphan-lambda-no-owner",
                "Unattributed function with a very low invocation count. "
                "Candidate for review; nothing executes in this demo.",
            ),
        ):
            self._approvals.create_ticket(
                resource_id,
                PotentialAction.STOP_RESOURCE,
                rationale=rationale,
                ticket_id=ticket_id,
            )

    # --- SwsBackend protocol ---
    def collect_workspace(
        self,
        *,
        regions: list[str],
        limit: int | None = None,
        collect_cost: bool = False,
        cost_window_days: int | None = None,
        cost_group_by: list[str] | None = None,
        cost_end_date: date | None = None,
    ) -> WorkspaceSnapshot:
        return self.snapshot

    def derive_relationships(
        self, snapshot: WorkspaceSnapshot
    ) -> list[ResourceRelationship]:
        return _derive_relationships(snapshot)

    def evaluate_workspace(
        self,
        snapshot: WorkspaceSnapshot,
        relationships: list[Any] | None = None,
    ) -> list[PolicyDecision]:
        return _evaluate_workspace(snapshot, relationships=relationships)

    def get_cost_estimates(
        self,
        *,
        end_date: date,
        window_days: int | None = None,
        group_by: list[str] | None = None,
    ) -> CostCollectionReport:
        return CostCollectionReport(
            estimates=demo_cost_estimates(),
            truncated=False,
            failures=[],
        )

    def explain(
        self, resource: ResourceRecord, decision: PolicyDecision
    ) -> ExplanationResult:
        return _demo_explanation(resource, decision)

    def list_approvals(self) -> list[ApprovalTicket]:
        return list(self._approvals.pending())

    def decide_ticket(
        self,
        ticket_id: str,
        *,
        decision: str,
        decided_by: str = "",
        reason: str = "",
    ) -> ApprovalTicket:
        if decision == "grant":
            return self._approvals.grant(
                ticket_id, decided_by=decided_by, reason=reason
            )
        return self._approvals.deny(
            ticket_id, decided_by=decided_by, reason=reason
        )

    def request_approval(
        self,
        *,
        resource_id: str,
        resource_type: SWSResourceType,
        action: PotentialAction,
        rationale: str = "",
    ) -> ActionPlan:
        # Delegates to the REAL ActionPlanner, so the demo exercises the
        # actual deterministic authorization gate over synthetic resources.
        return self._planner.plan(
            resource_id=resource_id,
            resource_type=resource_type,
            action=action,
            rationale=rationale,
        )