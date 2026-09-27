"""Start the M5 interactive demo: the real M4 MCP server on a synthetic
backend plus the web simulator, both locally and explicitly.

Run with::

    python -m sws_agent.simulator

Environment overrides:

    SWS_MCP_HOST / SWS_MCP_PORT   (default 127.0.0.1:8765) real M4 server
    SWS_SIM_HOST / SWS_SIM_PORT   (default 127.0.0.1:8080) web simulator
    SWS_MCP_URL                   if set, run the web app only, pointing at an
                                  already-running M4 Streamable HTTP endpoint.

The web app connects to the real M4 server over Streamable HTTP; the M4
server's backend is the deterministic synthetic demo dataset. Nothing ever
touches AWS or executes an action; nothing is left running after exit.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time

import uvicorn

from ..mcp.server import SwsMcpServer
from .app import create_app
from .demo import DemoBackend

MCP_DEFAULT_URL = "http://127.0.0.1:8765/mcp"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(
        f"timed out waiting for {host}:{port} to accept connections"
    )


def _run_uvicorn(server: uvicorn.Server) -> None:
    asyncio.run(server.serve())


def main(argv: list[str] | None = None) -> int:
    mcp_host = os.environ.get("SWS_MCP_HOST", "127.0.0.1")
    mcp_port = _env_int("SWS_MCP_PORT", 8765)
    web_host = os.environ.get("SWS_SIM_HOST", "127.0.0.1")
    web_port = _env_int("SWS_SIM_PORT", 8080)
    external_mcp_url = os.environ.get("SWS_MCP_URL", "").strip()

    if external_mcp_url:
        web_app = create_app(mcp_url=external_mcp_url)
        uvicorn.run(web_app, host=web_host, port=web_port, log_level="warning")
        return 0

    print(
        "\nSWS demo (M5): synthetic workspace only. No AWS access, "
        "no real cost data, no actions executed.",
        file=sys.stderr,
    )

    backend = DemoBackend()
    mcp_app = SwsMcpServer(backend=backend).streamable_http_app(path="/mcp")
    web_app = create_app(mcp_url=MCP_DEFAULT_URL)

    mcp_server = uvicorn.Server(
        uvicorn.Config(mcp_app, host=mcp_host, port=mcp_port, log_level="warning")
    )
    web_server = uvicorn.Server(
        uvicorn.Config(web_app, host=web_host, port=web_port, log_level="warning")
    )

    mcp_thread = threading.Thread(
        target=_run_uvicorn, args=(mcp_server,), daemon=True
    )
    mcp_thread.start()
    try:
        _wait_for_port(mcp_host, mcp_port)
    except RuntimeError as exc:
        print(f"error starting the M4 MCP server: {exc}", file=sys.stderr)
        mcp_server.should_exit = True
        return 2

    web_thread = threading.Thread(
        target=_run_uvicorn, args=(web_server,), daemon=True
    )
    web_thread.start()
    try:
        _wait_for_port(web_host, web_port)
    except RuntimeError as exc:
        print(f"error starting the web simulator: {exc}", file=sys.stderr)

    print(f"M4 Streamable HTTP MCP server : http://{mcp_host}:{mcp_port}/mcp", file=sys.stderr)
    print(f"Web simulator                  : http://{web_host}:{web_port}", file=sys.stderr)
    print(
        "Ctrl+C to stop BOTH servers cleanly.",
        file=sys.stderr,
    )

    try:
        while mcp_thread.is_alive() and web_thread.is_alive():
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        mcp_server.should_exit = True
        web_server.should_exit = True
        mcp_thread.join(timeout=5)
        web_thread.join(timeout=5)

    print("\nDemo stopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())