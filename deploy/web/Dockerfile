# SWS public demo website — container image for the M5 simulator.
#
# Serves the *existing* starlette web demo (src/sws_agent/simulator/) unchanged.
# The demo drives the real SWS MCP tool boundary (the official MCP SDK server)
# over an IN-PROCESS synthetic backend (DemoBackend). Therefore the SWS
# workspace is synthetic by construction:
#
#   * no AWS resource access, no credentials baked in, no live AWS inventory;
#   * no calls to mcp.sjsujalgupta.xyz or any AgentCore endpoint;
#   * no mutation path (decide_ticket is excluded from the agent tool catalog).
#
# The optional conversational layer (see SWS_AGENT_PROVIDER / SWS_AGENT_MODEL)
# is the REAL Strands + Amazon Bedrock provider: it is given only a hard-coded
# read-only tool catalog and may only produce prose. It is configured at
# runtime through Render environment/secrets (never built into the image). The
# only AWS call it can make is Bedrock inference; the Bedrock identity used on
# Render must be a separate, Bedrock-only identity (see the runbook), NOT the
# SWS execution role.
#
# The `simulator` extra is the demo runtime (starlette + the MCP SDK + uvicorn
# + httpx); the `explanation` extra adds `strands-agents` for the conversational
# provider. `strands-agents` pulls boto3 transitively (Bedrock is an AWS API),
# but the simulator code never imports boto3 or builds an AWS resource client:
# the SWS collectors are never used here because the backend is DemoBackend.
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

RUN pip install --no-cache-dir ".[simulator,explanation]"

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8080

ENTRYPOINT ["python", "-m", "sws_agent.simulator"]
