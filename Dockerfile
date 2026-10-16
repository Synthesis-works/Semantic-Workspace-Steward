# SWS M17 — container image for the authenticated MCP server.
#
# Serves the existing SWS MCP boundary (src/sws_agent/mcp/server.py) over the
# Streamable HTTP transport on 0.0.0.0:8000/mcp. The endpoint's credential
# gate (src/sws_agent/mcp/endpoint_auth.py) is opt-in and activates only when
# SWS_MCP_API_KEY_SECRET_ARN is set in the task environment. No mutation path
# is exposed here: the image ships the read-only MCP surface unchanged.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Only what the runtime install needs; the hermetic test suite is not shipped.
COPY pyproject.toml README.md ./
COPY src ./src

# Base + the two runtime extras the container actually uses:
#   aws -> boto3 (lazy collectors + Secrets Manager read for the endpoint gate)
#   mcp -> official MCP SDK v2 + uvicorn (Streamable HTTP transport)
RUN pip install --no-cache-dir ".[aws,mcp]"

# Run as an unprivileged user; the server writes nothing to disk.
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000

# SWS_AWS_REGION selects real-AWS mode (the collectors use the ECS task role
# via the container credential provider; no profile/static keys are baked in).
ENV SWS_AWS_REGION=us-east-1

ENTRYPOINT ["python", "-m", "sws_agent.mcp.server"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--path", "/mcp"]
