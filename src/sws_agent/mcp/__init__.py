"""MCP boundary for SWS.

SWS exposes its orchestration through a real MCP server using the
Streamable HTTP transport (``mcp/server.py``). The MCP layer is a thin
adapter over SWS business logic: policy, authorization, and action logic
never live here. ``SwsMcpServer`` consumes the dependency-free
``ToolRegistry`` defined in this module and wires each registered tool
into the Streamable HTTP server; the simulator runs the same server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


class MCPServer(Protocol):
    """Boundary implemented by the real MCP server in ``mcp/server.py``.

    ``transport`` selects the MCP transport; SWS targets Streamable HTTP.
    """

    def add_tool(self, name: str, handler: ToolHandler) -> None: ...
    def serve(self, *, transport: str = "streamable-http") -> None: ...


@dataclass
class ToolRegistry:
    """Dependency-free registry mapping tool names to handler functions.

    Consumed by ``SwsMcpServer`` (``mcp/server.py``), which wires each
    registered handler into the Streamable HTTP server. Kept SDK-free so
    the dispatch logic stays testable without the MCP SDK.
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