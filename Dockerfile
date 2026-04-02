FROM python:3.11-slim

RUN apt-get update -y && \
    apt-get install -y ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir uv

# Install dependencies before copying source so this layer is cached
# as long as pyproject.toml doesn't change.
# Read deps directly from pyproject.toml so the Dockerfile never drifts.
COPY pyproject.toml /app/
RUN python3 -c "\
import tomllib, subprocess; \
deps = tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; \
subprocess.run(['uv','pip','install','--system'] + deps, check=True)"

COPY agent/ /app/agent/

RUN addgroup --gid 1002 --system agentapp && \
    adduser --system agentapp --uid 1002 --ingroup agentapp && \
    chown -R agentapp:agentapp /app

USER agentapp

CMD ["python", "-m", "uvicorn", "agent.api:app", "--host", "0.0.0.0", "--port", "8001"]
