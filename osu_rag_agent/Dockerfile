FROM python:3.11-slim
WORKDIR /app

# Create non-root user
RUN adduser --disabled-password --gecos "" appuser
USER appuser
ENV PATH="/home/appuser/.local/bin:$PATH"

# Install dependencies first (layer cache friendly)
COPY --chown=appuser:appuser requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ADK api_server expects agents under /app/agents/<agent_name>/
# Copy this folder's contents into the correct nested path
COPY --chown=appuser:appuser . /app/agents/osu_rag_agent/

# Parent of the agents/ directory must be on PYTHONPATH so
# `import osu_rag_agent` resolves correctly inside the server
ENV PYTHONPATH="/app"

EXPOSE 8080

# --a2a enables the A2A protocol routes:
#   /.well-known/agent-card.json  (top-level, served per agent)
#   /a2a/<agent_name>/            (RPC endpoint)
CMD ["adk", "api_server", "--port=8080", "--host=0.0.0.0", "--a2a", "/app/agents"]
