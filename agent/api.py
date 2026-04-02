"""FastAPI entry point for the SRE agent.

Startup wires up config and telemetry once; the agent instance is reused
across requests.

    uvicorn agent.api:app --host 0.0.0.0 --port 8001
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .agent import Agent
from .config import Config, load_config
from .models import Task
from .observability.tracing import Telemetry


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class TaskRequest(BaseModel):
    task_type: str
    session_id: str
    user_id: str
    prompt: str


class TaskResponse(BaseModel):
    task_id: str
    output: str
    success: bool
    step_count: int = 0
    tokens_total: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

_agent: Optional[Agent] = None
_config: Optional[Config] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _config
    _config = load_config()
    Telemetry.configure(_config)
    _agent = Agent.create(_config)
    yield
    _agent = None


app = FastAPI(title="agent-sre", lifespan=lifespan, root_path="/agent-sre")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/tasks", response_model=TaskResponse)
def run_task(request: TaskRequest) -> TaskResponse:
    task = Task(
        task_type=request.task_type,
        session_id=request.session_id,
        user_id=request.user_id,
        prompt=request.prompt,
    )
    result = _agent.run_task(task)
    return TaskResponse(
        task_id=result.task_id,
        output=result.output,
        success=result.success,
        step_count=result.step_count,
        tokens_total=result.tokens_total,
        error=result.error,
    )


@app.get("/health")
def health():
    """Liveness probe — returns 200 as long as the process is alive."""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Readiness probe — returns 200 once config and agent are initialised."""
    if _agent is None:
        raise HTTPException(status_code=503, detail="agent not initialised")
    if not _config or not _config.anthropic_api_key:
        raise HTTPException(status_code=503, detail="missing ANTHROPIC_API_KEY")
    return {"status": "ok"}
