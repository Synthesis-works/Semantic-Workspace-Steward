"""Deterministic, rule-based intent router for the M5 demo (no LLM).

A small phrase/keyword vocabulary maps user messages to ``Command`` objects.
The router is pure and deterministic: the same text always yields the same
command. It never pretends to be a full conversational engine; anything it
cannot map deterministically becomes ``unknown``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_INTENT = "unknown"

_DECIDE_TOKENS = frozenset(
    {"approve", "approving", "grant", "granted", "deny", "denied", "reject"}
)
_GRANT_TOKENS = frozenset({"approve", "approving", "grant", "granted", "yes"})
_DENY_TOKENS = frozenset({"deny", "denied", "reject", "no"})
_AUDIT_PHRASES = (
    "audit",
    "collect my workspace",
    "gather inventory",
    "scan my workspace",
    "run an audit",
)
_EXPLAIN_PHRASES = (
    "why",
    "explain",
    "what is",
    "what does",
    "reason",
    "matter",
)
_APPROVALS_PHRASES = (
    "approval",
    "approvals",
    "pending",
    "waiting for approval",
    "ticket list",
)
_ATTENTION_PHRASES = (
    "need attention",
    "needs attention",
    "attention",
    "flagged",
    "issues",
    "problems",
    "what resources",
    "which resources",
    "action needed",
)
_HELP_PHRASES = ("help", "what can you do", "commands", "how do i", "usage")

_TICKET_ID_RE = re.compile(r"\b(demo-ticket-\d+|[a-z0-9-]{16,})\b")


@dataclass(frozen=True)
class Command:
    """A parsed demo request the simulator can execute deterministically."""

    intent: str
    resource_id: str | None = None
    ticket_id: str | None = None
    decision: str | None = None


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", text) if token}


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _extract_ticket_id(text: str) -> str | None:
    match = _TICKET_ID_RE.search(text)
    return match.group(1) if match else None


def _extract_resource(
    text: str, references: list[dict[str, list[str]]]
) -> str | None:
    for reference in references:
        if any(keyword in text for keyword in reference["keywords"]):
            return reference["id"]
    return None


def route(
    message: str,
    *,
    resource_references: list[dict[str, list[str]]] | None = None,
) -> Command:
    """Map a free-form demo message to a deterministic ``Command``."""
    from .demo import demo_resource_references

    refs = (
        resource_references
        if resource_references is not None
        else demo_resource_references()
    )
    text = (message or "").strip().lower()
    if not text:
        return Command(intent="unknown")

    tokens = _tokens(text)
    ticket_id = _extract_ticket_id(text)

    if ticket_id is not None and not tokens.isdisjoint(_DECIDE_TOKENS):
        if not tokens.isdisjoint(_DENY_TOKENS):
            return Command(
                intent="decide", ticket_id=ticket_id, decision="deny"
            )
        return Command(intent="decide", ticket_id=ticket_id, decision="grant")

    if _contains_any(text, _AUDIT_PHRASES) and "attention" not in text:
        return Command(intent="audit")

    if _contains_any(text, _EXPLAIN_PHRASES):
        return Command(
            intent="explain", resource_id=_extract_resource(text, refs)
        )

    if not tokens.isdisjoint(_DECIDE_TOKENS):
        if not tokens.isdisjoint(_DENY_TOKENS):
            return Command(intent="decide", decision="deny")
        return Command(intent="decide", decision="grant")

    if _contains_any(text, _APPROVALS_PHRASES):
        return Command(intent="approvals")

    if _contains_any(text, _ATTENTION_PHRASES):
        return Command(intent="attention")

    if _contains_any(text, _HELP_PHRASES):
        return Command(intent="help")

    return Command(intent=DEFAULT_INTENT)