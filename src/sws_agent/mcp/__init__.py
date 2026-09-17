"""MCP boundary for SWS.

SWS exposes its orchestration through a real MCP server using the
Streamable HTTP transport. The MCP layer is a thin adapter over SWS
business logic: policy, authorization, and action logic never live here.

Nothing in this module claims MCP compliance. The dependency-free
``ToolRegistry`` prepares the integration and is genuinely tested;
wiring an actual MCP SDK server on top of it is a future task that must
be implemented and tested before any compliance is claimed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


class MCPServer(Protocol):
    """Boundary for the eventual real MCP server.

    ``transport`` selects the MCP transport; SWS targets Streamable HTTP.
    """

    def add_tool(self, name: str, handler: ToolHandler) -> None: ...
    def serve(self, *, transport: str = "streamable-http") -> None: ...


@dataclass
class ToolRegistry:
    """Dependency-free registry mapping tool names to handler functions.

    Testable without the MCP SDK. A future real MCP server consumes this
    registry and wires each handler into the Streamable HTTP server.
    """

    _handlers: dict[str, ToolHandler] = field(default_factory=dict)

    def register(self, name: str, handler: ToolHandler) -> None:
        if not name or not name.strip():
            raise ValueError("tool name must be non-empty")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handlers[name] = handler

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"unknown tool: {name}")
        return handler(arguments)

    def names(self) -> list[str]:
        """Registered tool names, sorted for stable output."""
        return sorted(self._handlers)