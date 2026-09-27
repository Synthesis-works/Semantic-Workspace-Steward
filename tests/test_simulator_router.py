"""Hermetic tests for the deterministic demo intent router.

The router is pure and rule-based: same input, same command, every time. No
MCP server or AWS involvement.
"""

from sws_agent.simulator.router import Command, route


def test_empty_message_is_unknown() -> None:
    assert route("") == Command(intent="unknown")
    assert route("   ") == Command(intent="unknown")


def test_unknown_input_is_deterministic_and_never_matches() -> None:
    first = route("do a barrel roll")
    second = route("do a barrel roll")
    assert first == second == Command(intent="unknown")


def test_audit_intent() -> None:
    assert route("Audit my AWS workspace") == Command(intent="audit")
    assert route("please run an audit") == Command(intent="audit")


def test_audit_words_do_not_override_needs_attention() -> None:
    result = route("audit what resources need attention")
    assert result.intent == "attention"


def test_explain_intent_resolves_demo_resource() -> None:
    result = route("Why does ghost-bucket-no-owner matter?")
    assert result.intent == "explain"
    assert result.resource_id == "ghost-bucket-no-owner"


def test_explain_intent_without_resource() -> None:
    result = route("explain")
    assert result.intent == "explain"
    assert result.resource_id is None


def test_approvals_intent() -> None:
    assert route("Show my pending approvals").intent == "approvals"
    assert route("what approvals are pending").intent == "approvals"


def test_decide_grant_with_ticket_id() -> None:
    result = route("Approve this ticket demo-ticket-0001")
    assert result.intent == "decide"
    assert result.ticket_id == "demo-ticket-0001"
    assert result.decision == "grant"


def test_decide_deny_with_ticket_id() -> None:
    result = route("Deny the ticket demo-ticket-0002")
    assert result.intent == "decide"
    assert result.ticket_id == "demo-ticket-0002"
    assert result.decision == "deny"


def test_decide_grant_without_ticket_id() -> None:
    result = route("approve")
    assert result.intent == "decide"
    assert result.ticket_id is None
    assert result.decision == "grant"


def test_decide_deny_without_ticket_id() -> None:
    result = route("deny all")
    assert result.intent == "decide"
    assert result.decision == "deny"


def test_attention_intent() -> None:
    assert route("What resources need attention?").intent == "attention"
    assert route("which resources are flagged").intent == "attention"


def test_help_intent() -> None:
    assert route("help").intent == "help"
    assert route("what can you do").intent == "help"


def test_decide_words_do_not_hijack_approval_list() -> None:
    result = route("Show my pending approvals")
    assert result.intent == "approvals"