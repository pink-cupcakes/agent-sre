from __future__ import annotations

import logging
import sys
import time
from typing import Optional

from agent.observability.tracer import tracer

from agent.config import Config, load_config
from agent.llm import LLMClient
from agent.memory.session import SessionStore
from agent.memory.store import MemoryStore
from agent.models import Task, TaskResult
from agent.observability import context as ctx
from agent.observability import metrics
from agent.orchestration import Orchestrator
from agent.tools import ToolRegistry, default_tools

logger = logging.getLogger(__name__)


class Agent:
    def __init__(self, orchestrator: Orchestrator, config: Config) -> None:
        self._orchestrator = orchestrator
        self._config = config

    @classmethod
    def create(cls, config: Optional[Config] = None) -> "Agent":
        cfg = config or load_config()
        llm = LLMClient(cfg)
        sessions = SessionStore()
        memory = MemoryStore()
        tools: ToolRegistry = default_tools()
        orchestrator = Orchestrator(cfg, llm, tools, sessions, memory)
        return cls(orchestrator, cfg)

    def run_task(self, task: Task) -> TaskResult:
        """
        Execute a task.

        Produces exactly one root span named "agent.task" with the four
        required tags: task.id, task.type, session.id, user.id.
        """
        ctx.task_id_var.set(task.task_id)
        ctx.session_id_var.set(task.session_id)

        task_start = time.monotonic()

        with tracer.trace("agent.task") as span:
            span.set_tag("task.id", task.task_id)
            span.set_tag("task.type", task.task_type)
            span.set_tag("session.id", task.session_id)
            span.set_tag("user.id", task.user_id)

            try:
                result = self._orchestrator.run(task)

                span.set_tag("task.success", result.success)
                span.set_tag("task.step_count", result.step_count)
                span.set_tag("task.tokens_total", result.tokens_total)

                if not result.success:
                    span.error = 1
                    span.set_tag("error.message", result.error or "unknown error")

                duration_ms = (time.monotonic() - task_start) * 1000
                status = "success" if result.success else "error"
                model_tag = f"model:{self._config.llm_model}"
                task_type_tag = f"task_type:{task.task_type}"

                metrics.distribution(
                    "agent.task.duration_ms",
                    duration_ms,
                    tags=[task_type_tag, f"status:{status}"],
                )
                metrics.distribution(
                    "agent.task.step_count",
                    result.step_count,
                    tags=[task_type_tag],
                )
                metrics.distribution(
                    "agent.task.tokens.total",
                    result.tokens_total,
                    tags=[task_type_tag, model_tag],
                )
                metrics.distribution(
                    "agent.task.cost_usd",
                    result.cost_usd,
                    tags=[task_type_tag, model_tag],
                )

                logger.info(
                    "task run completed",
                    extra={
                        "event": "agent.task.completed",
                        "task.id": task.task_id,
                        "task.type": task.task_type,
                        "task.success": result.success,
                        "task.step_count": result.step_count,
                        "task.tokens_total": result.tokens_total,
                        "task.cost_usd": result.cost_usd,
                        "task.duration_ms": round(duration_ms, 2),
                    },
                )

                return TaskResult(
                    task_id=task.task_id,
                    output=result.output,
                    success=result.success,
                    step_count=result.step_count,
                    tokens_total=result.tokens_total,
                    error=result.error,
                )

            except Exception as exc:
                span.error = 1
                span.set_exc_info(*sys.exc_info())
                logger.exception(
                    "unhandled exception in task",
                    extra={
                        "event": "agent.task.failed",
                        "task.id": task.task_id,
                    },
                )
                return TaskResult(
                    task_id=task.task_id,
                    output="",
                    success=False,
                    error=str(exc),
                )
