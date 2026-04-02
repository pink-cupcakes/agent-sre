from __future__ import annotations

import logging
import sys
from typing import Optional

from ddtrace import tracer

from agent.config import Config, load_config
from agent.llm import LLMClient
from agent.memory.session import SessionStore
from agent.memory.store import MemoryStore
from agent.models import Task, TaskResult
from agent.orchestration import Orchestrator
from agent.tools import ToolRegistry, default_tools

logger = logging.getLogger(__name__)


class Agent:
    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator

    @classmethod
    def create(cls, config: Optional[Config] = None) -> "Agent":
        cfg = config or load_config()
        llm = LLMClient(cfg)
        sessions = SessionStore()
        memory = MemoryStore()
        tools: ToolRegistry = default_tools()
        orchestrator = Orchestrator(cfg, llm, tools, sessions, memory)
        return cls(orchestrator)

    def run_task(self, task: Task) -> TaskResult:
        """
        Execute a task.

        Produces exactly one root span named "agent.task" with the four
        required tags: task.id, task.type, session.id, user.id.
        """
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
                logger.exception("Unhandled exception in task %s", task.task_id)
                return TaskResult(
                    task_id=task.task_id,
                    output="",
                    success=False,
                    error=str(exc),
                )
