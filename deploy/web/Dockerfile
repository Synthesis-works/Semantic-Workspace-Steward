# SWS public demo website — container image for the M5 simulator.
#
# Serves the *existing* starlette web demo (src/sws_agent/simulator/) unchanged.
# The demo drives the real SWS MCP tool boundary (the official MCP SDK server)
# over an IN-PROCESS synthetic backend (DemoBackend). Therefore this image is
# synthetic-only by construction:
#
#   * no AWS access, no boto3, no AWS credentials, no secrets, no live calls;
#   * no calls to mcp.sjsujalgupta.xyz or any AgentCore endpoint;
#   * no mutation path (decide_ticket is excluded from the agent tool catalog).
#
# The `simulator` extra is exactly what the demo needs (starlette + the MCP SDK
# + uvicorn + httpx). boto3 (`aws` extra) is deliberately NOT installed.
#
# Binds 0.0.0.0 and honors the platform PORT env var (resolved in
# src/sws_agent/simulator/__main__.py) so the same image runs unchanged on a
# free container host (Render). The bundled MCP server stays on loopback only.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SWS_SIM_HOST=0.0.0.0

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir ".[simulator]"

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8080

ENTRYPOINT ["python", "-m", "sws_agent.simulator"]
