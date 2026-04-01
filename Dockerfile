FROM python:3.11-slim

RUN apt-get update -y && \
    apt-get install -y ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir uv

# Install dependencies before copying source so this layer is cached
# as long as pyproject.toml doesn't change.
COPY pyproject.toml /app/
RUN uv pip install --system \
    "anthropic>=0.30.0" \
    "ddtrace>=2.20.1" \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.29.0" \
    "python-dotenv>=1.0.0"

COPY agent/ /app/agent/

RUN addgroup --gid 1002 --system agentapp && \
    adduser --system agentapp --uid 1002 --ingroup agentapp && \
    chown -R agentapp:agentapp /app

USER agentapp

CMD ["python", "-m", "uvicorn", "agent.api:app", "--host", "0.0.0.0", "--port", "8001"]
