FROM python:3.11-slim
WORKDIR /app

# Create non-root user
RUN adduser --disabled-password --gecos "" appuser
USER appuser
ENV PATH="/home/appuser/.local/bin:$PATH"

# Install dependencies
COPY --chown=appuser:appuser requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy agent source
COPY --chown=appuser:appuser . /app/osu_rag_agent/

# The ADK api_server expects the agent package to be importable.
# Expose the parent directory so `import osu_rag_agent` works.
ENV PYTHONPATH="/app"

EXPOSE 8080

# Start the ADK API server (serves /run, /run_sse, and /.well-known/agent-card.json)
CMD ["adk", "api_server", "--port=8080", "--host=0.0.0.0", "/app"]
