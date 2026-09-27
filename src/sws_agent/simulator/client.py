"""MCP client seam for the M5 simulator.

The simulator talks to the REAL M4 Streamable HTTP MCP server, never to a
fake endpoint. ``LiveMcpClient`` wraps the official MCP SDK v2 client
(``mcp.client.streamable_http`` + ``mcp.client.session.ClientSession``).

``McpClient`` is the seam hermetic service tests use with a fake that
mirrors the SDK result shapes; the production path is always ``LiveMcpClient``
over the official Streamable HTTP transport.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


@runtime_checkable
class McpClient(Protocol):
    """A minimal client sufficient to drive the M4 server."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def list_tools(self) -> list[dict[str, str]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class LiveMcpClient:
    """Official Streamable HTTP MCP client backed by the MCP SDK v2."""

    def __init__(
        self,
        url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._http_client = http_client
        self._streams_ctx = None
        self._session: ClientSession | None = None
        self.server_info: dict[str, str] = {}

    async def connect(self) -> None:
        if self._session is not None:
            return
        if self._http_client is None:
            self._http_client = httpx.AsyncClient()
        self._streams_ctx = streamable_http_client(
            self._url, http_client=self._http_client
        )
        read, write = await self._streams_ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        init = await self._session.initialize()
        self.server_info = {
            "name": init.server_info.name,
            "version": init.server_info.version,
        }

    async def close(self) -> None:
        session, streams_ctx = self._session, self._streams_ctx
        self._session = None
        self._streams_ctx = None
        if session is not None:
            await session.__aexit__(None, None, None)
        if streams_ctx is not None:
            await streams_ctx.__aexit__(None, None, None)
        if self._http_client is not None:
            await self._http_client.aclose()

    async def list_tools(self) -> list[dict[str, str]]:
        result = await self._session.list_tools()
        return [
            {"name": tool.name, "description": tool.description or ""}
            for tool in result.tools
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Any:
        return await self._session.call_tool(name, arguments or {})