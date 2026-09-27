"""Starlette web layer for the M5 interactive demo.

Only composes routes and lifespan behavior; all behavior lives in
``service.Simulator`` over the real M4 MCP server. Importing this module
never binds a socket nor connects to anything — the app serves only when an
ASGI server runs it (``python -m sws_agent.simulator``).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .client import LiveMcpClient
from .service import Simulator

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")

_DEMO_COMMANDS = {
    "commands": [
        "Audit my AWS workspace",
        "What resources need attention?",
        "Why does web-assets-cdn matter?",
        "Explain orphan-lambda-no-owner",
        "Show my pending approvals",
        "Approve this ticket demo-ticket-0001",
        "Deny this ticket demo-ticket-0002",
        "help",
    ]
}


def create_app(
    mcp_url: str | None = None,
    *,
    simulator: Simulator | None = None,
) -> Starlette:
    """Build the demo web app.

    ``simulator`` is injectable for hermetic tests (with a fake MCP client);
    otherwise a live ``Simulator`` over ``mcp_url`` (defaulting to the
    standard M4 Streamable HTTP endpoint) is constructed and connected on
    startup.
    """
    owns_thread = simulator is None
    if simulator is None:
        url = mcp_url or os.environ.get(
            "SWS_MCP_URL", "http://127.0.0.1:8765/mcp"
        )
        simulator = Simulator(LiveMcpClient(url))

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[dict[str, Any]]:
        await simulator.connect()
        try:
            yield {}
        finally:
            await simulator.close()

    async def index(request: Any) -> FileResponse:
        return FileResponse(INDEX_HTML)

    async def api_chat(request: Any) -> JSONResponse:
        body = await request.json()
        message = (body or {}).get("message", "")
        history = (body or {}).get("history") or None
        response = await simulator.chat(message, history=history)
        return JSONResponse(response.to_dict())

    async def api_commands(request: Any) -> JSONResponse:
        return JSONResponse(_DEMO_COMMANDS)

    async def api_meta(request: Any) -> JSONResponse:
        return JSONResponse(
            {
                "demo": True,
                "service": "SWS interactive demo (M5)",
                "mcp_endpoint": os.environ.get(
                    "SWS_MCP_URL", "http://127.0.0.1:8765/mcp"
                ),
            }
        )

    async def api_tools(request: Any) -> JSONResponse:
        tools = await simulator.list_available_tools()
        return JSONResponse({"tools": tools})

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/chat", api_chat, methods=["POST"]),
            Route("/api/commands", api_commands, methods=["GET"]),
            Route("/api/meta", api_meta, methods=["GET"]),
            Route("/api/tools", api_tools, methods=["GET"]),
            Mount("/static", app=StaticFiles(directory=STATIC_DIR)),
        ],
        lifespan=lifespan,
        exception_handlers={500: _error_500},
    )
    app.state.simulator = simulator
    if owns_thread:
        app.state.live = True
    return app


async def _error_500(request: Any, exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": "internal error", "detail": str(exc)}, status_code=500
    )