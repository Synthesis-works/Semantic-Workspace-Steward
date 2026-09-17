"""Dependency-free MCP tool registry.

Tests pin that registration and dispatch work without the MCP SDK. The
eventual MCP server consumes this registry; no MCP compliance is claimed
here.
"""

from __future__ import annotations

import pytest

from sws_agent.mcp import ToolRegistry


def test_register_and_invoke():
    registry = ToolRegistry()

    def ping(arguments):
        return {"pong": arguments.get("value")}

    registry.register("ping", ping)
    assert registry.invoke("ping", {"value": 7}) == {"pong": 7}


def test_unknown_tool_raises_key_error():
    registry = ToolRegistry()
    with pytest.raises(KeyError):
        registry.invoke("missing", {})


def test_empty_tool_name_rejected():
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register("", lambda arguments: {})


def test_non_callable_handler_rejected():
    registry = ToolRegistry()
    with pytest.raises(TypeError):
        registry.register("not_a_func", "not callable")


def test_names_returned_sorted():
    registry = ToolRegistry()
    registry.register("zeta", lambda arguments: {})
    registry.register("alpha", lambda arguments: {})
    assert registry.names() == ["alpha", "zeta"]