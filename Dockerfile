# Multi-stage build for the LCH Reporting MCP server.
# Stage 1 installs deps into a venv; stage 2 is a slim runtime.

FROM python:3.12-slim AS builder
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim AS runtime
# Non-root user (least privilege - good to mention in interview)
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    MCP_PORT=8080 \
    DATA_BACKEND=s3
COPY --from=builder /opt/venv /opt/venv
COPY mcp_server/ /app/mcp_server/
# The in-process data APIs (reference rates + legal entities) the MCP tools call.
COPY apis/ /app/apis/
# Bundle the sample data so the container also runs standalone with DATA_BACKEND=local
COPY data/ /app/data/
USER appuser
EXPOSE 8080
# Simple healthcheck hits the HTTP port
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import socket,os; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1', int(os.environ.get('MCP_PORT','8080')))); s.close()" || exit 1
CMD ["python", "mcp_server/server.py"]
