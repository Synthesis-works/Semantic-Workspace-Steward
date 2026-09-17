"""Minimal in-memory human-approval ticket store.

Human approval is the boundary for future side-effecting actions. This
module models approval tickets and their state machine only: it performs
no persistence, no AWS calls, and no action execution. Time is injectable
so TTL expiration is deterministically testable without sleeping.

Valid transitions (enforced by the store):
    PENDING -> GRANTED
    PENDING -> DENIED
    PENDING -> EXPIRED
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Final

from .constants import ApprovalStatus, PotentialAction
from .models import ApprovalTicket

DEFAULT_APPROVAL_TICKET_TTL: Final[timedelta] = timedelta(hours=24)
"""Default time-to-live for a pending approval ticket."""


class ApprovalError(Exception):
    """Base class for approval-store failures."""


class UnknownTicketError(ApprovalError):
    """Raised when a ticket id is not present in the store."""


class InvalidTransitionError(ApprovalError):
    """Raised when a ticket is moved to a status not reachable from PENDING."""


class InMemoryApprovalStore:
    """Deterministic, in-memory approval ticket state machine.

    The store is not durable; an injected clock and TTL make expiration
    behavior deterministic and testable. Tickets may transition away from
    PENDING exactly once.
    """

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_APPROVAL_TICKET_TTL,
    ) -> None:
        self._now: Callable[[], datetime] = now or (
            lambda: datetime.now(timezone.utc)
        )
        self._ttl = ttl
        self._tickets: dict[str, ApprovalTicket] = {}

    def create_ticket(
        self,
        resource_id: str,
        action: PotentialAction,
        rationale: str = "",
        ticket_id: str | None = None,
    ) -> ApprovalTicket:
        ticket = ApprovalTicket(
            ticket_id=ticket_id or uuid.uuid4().hex,
            resource_id=resource_id,
            action=action,
            rationale=rationale,
            created_at=self._now(),
        )
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def get(self, ticket_id: str) -> ApprovalTicket:
        self._require_known(ticket_id)
        self._expire_if_stale(ticket_id)
        return self._tickets[ticket_id]

    def grant(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
    ) -> ApprovalTicket:
        return self._resolve(
            ticket_id, ApprovalStatus.GRANTED, decided_by, reason
        )

    def deny(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
    ) -> ApprovalTicket:
        return self._resolve(
            ticket_id, ApprovalStatus.DENIED, decided_by, reason
        )

    def expire(
        self,
        ticket_id: str,
        *,
        decided_by: str = "",
        reason: str = "",
    ) -> ApprovalTicket:
        return self._resolve(
            ticket_id, ApprovalStatus.EXPIRED, decided_by, reason
        )

    def pending(self) -> list[ApprovalTicket]:
        """Return tickets still awaiting approval.

        Pending tickets beyond the TTL are expired first, so expired
        tickets are excluded from the result deterministically.
        """
        for ticket_id in list(self._tickets):
            self._expire_if_stale(ticket_id)
        return [
            ticket
            for ticket in self._tickets.values()
            if ticket.status is ApprovalStatus.PENDING
        ]

    def _resolve(
        self,
        ticket_id: str,
        new_status: ApprovalStatus,
        decided_by: str,
        reason: str,
    ) -> ApprovalTicket:
        self._require_known(ticket_id)
        self._expire_if_stale(ticket_id)
        ticket = self._tickets[ticket_id]
        if ticket.status is not ApprovalStatus.PENDING:
            raise InvalidTransitionError(
                f"ticket '{ticket_id}' cannot transition from "
                f"{ticket.status.value} to {new_status.value}"
            )
        self._tickets[ticket_id] = ticket.model_copy(
            update={
                "status": new_status,
                "decided_at": self._now(),
                "decided_by": decided_by,
                "decision_reason": reason,
            }
        )
        return self._tickets[ticket_id]

    def _require_known(self, ticket_id: str) -> None:
        if ticket_id not in self._tickets:
            raise UnknownTicketError(f"unknown approval ticket: {ticket_id}")

    def _expire_if_stale(self, ticket_id: str) -> None:
        ticket = self._tickets[ticket_id]
        if ticket.status is not ApprovalStatus.PENDING:
            return
        if self._now() - ticket.created_at <= self._ttl:
            return
        self._tickets[ticket_id] = ticket.model_copy(
            update={
                "status": ApprovalStatus.EXPIRED,
                "decided_at": self._now(),
                "decision_reason": f"ticket expired after {self._ttl}",
            }
        )