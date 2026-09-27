"""M5 interactive web simulator over the real M4 MCP server.

The simulator is a local, demo-only web layer. It never reimplements SWS
behavior: the web app talks to the *real* M4 Streamable HTTP MCP server
(``sws_agent.mcp.server.SwsMcpServer``), and the server's backend is a
deterministic synthetic dataset (``sws_agent.simulator.demo``) that feeds
real deterministic SWS core functions (policy, relationships, approval).
"""

from .demo import DemoBackend, build_demo_snapshot
from .service import Simulator

__all__ = ["DemoBackend", "Simulator", "build_demo_snapshot"]