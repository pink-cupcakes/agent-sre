"""
Orchestrator — drives the decide → act → observe loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ddtrace import tracer

from agent.config import Config
from agent.llm import LLMClient, LLMResponse
from agent.llm.pricing import get_cost_usd
from agent.memory.session import SessionStore
from agent.memory.store import MemoryStore
from agent.models import Task
from agent.observability import context as ctx
from agent.observability import metrics
from agent.tools import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class StepRecord:
    step_number: int
    decision_type: str        # "tool_call" | "final_answer" | "error"
    tool_name: str | None
    llm_response: LLMResponse | None = None


@dataclass
class OrchestrationResult:
    output: str
    success: bool
    step_count: int
    steps: list[StepRecord] = field(default_factory=list)
    error: str | None = None
    tokens_total: int = 0
    cost_usd: float = 0.0


class Orchestrator:
    SYSTEM_PROMPT = (
        "You are a helpful SRE assistant. Use the available tools to complete "
        "the user's task. When you have a final answer, respond directly without "
        "using any tools."
    )

    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        tools: ToolRegistry,
        sessions: SessionStore,
        memory: MemoryStore,
    ) -> None:
        self._config = config
        self._llm = llm
        self._tools = tools
        self._sessions = sessions
        self._memory = memory

    def run(self, task: Task) -> OrchestrationResult:
        ctx.task_id_var.set(task.task_id)
        ctx.session_id_var.set(task.session_id)

        session = self._sessions.get_or_create(task.session_id, task.user_id)
        session.add_message("user", task.prompt)

        metrics.gauge("agent.session.active", self._sessions.count())

        logger.info(
            "task started",
            extra={
                "event": "task.started",
                "task.id": task.task_id,
                "task.type": task.task_type,
                "user.id": task.user_id,
            },
        )

        steps: list[StepRecord] = []
        tokens_total = 0
        cost_total_usd = 0.0

        for step_number in range(1, self._config.max_steps + 1):
            with tracer.trace("agent.step") as step_span:
                step_span.set_tags({"step.number": str(step_number)})

                try:
                    llm_response = self._llm.call(
                        messages=session.messages,
                        system=self.SYSTEM_PROMPT,
                        tools=self._tools.definitions() or None,
                    )
                except Exception as exc:
                    step_span.set_tags({"step.decision_type": "error"})
                    step_span.set_traceback()
                    logger.exception(
                        "llm call failed",
                        extra={
                            "event": "task.failed",
                            "step.number": step_number,
                            "step.decision_type": "error",
                            "step.tokens_accumulated": tokens_total,
                        },
                    )
                    steps.append(StepRecord(step_number=step_number, decision_type="error", tool_name=None))
                    return OrchestrationResult(
                        output="", success=False, step_count=step_number,
                        steps=steps, error=str(exc), tokens_total=tokens_total,
                        cost_usd=cost_total_usd,
                    )

                step_cost = get_cost_usd(
                    llm_response.model, llm_response.tokens_prompt, llm_response.tokens_completion
                )
                tokens_total += llm_response.tokens_total
                cost_total_usd += step_cost

                if llm_response.is_final_answer:
                    decision_type = "final_answer"
                    step_span.set_tags({"step.decision_type": decision_type})
                    steps.append(StepRecord(
                        step_number=step_number, decision_type=decision_type,
                        tool_name=None, llm_response=llm_response,
                    ))
                    session.add_message("assistant", llm_response.content)
                    self._sessions.save(session)
                    logger.info(
                        "task completed",
                        extra={
                            "event": "task.completed",
                            "step.number": step_number,
                            "step.decision_type": decision_type,
                            "step.tokens": llm_response.tokens_total,
                            "step.tokens_accumulated": tokens_total,
                            "step.cost_usd": step_cost,
                            "step.cost_accumulated_usd": cost_total_usd,
                            "step.latency_ms": round(llm_response.latency_ms, 2),
                        },
                    )
                    return OrchestrationResult(
                        output=llm_response.content, success=True,
                        step_count=step_number, steps=steps, tokens_total=tokens_total,
                        cost_usd=cost_total_usd,
                    )

                if llm_response.is_tool_call:
                    first_tool = llm_response.tool_calls[0]["name"] if llm_response.tool_calls else None
                    decision_type = "tool_call"
                    step_span.set_tags({"step.decision_type": decision_type, "step.tool_name": first_tool or ""})
                    logger.info(
                        "step decided: tool call",
                        extra={
                            "event": "step.decided",
                            "step.number": step_number,
                            "step.decision_type": decision_type,
                            "step.tool_name": first_tool,
                            "step.tokens": llm_response.tokens_total,
                            "step.tokens_accumulated": tokens_total,
                            "step.cost_usd": step_cost,
                            "step.cost_accumulated_usd": cost_total_usd,
                            "step.latency_ms": round(llm_response.latency_ms, 2),
                        },
                    )
                    tool_results_for_llm = [self._execute_tool(tc, step_number, steps) for tc in llm_response.tool_calls]

                    session.add_message("assistant", [
                        {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
                        for tc in llm_response.tool_calls
                    ])
                    session.add_message("user", tool_results_for_llm)

        logger.warning(
            "task exceeded max steps",
            extra={
                "event": "task.failed",
                "step.tokens_accumulated": tokens_total,
                "step.cost_accumulated_usd": cost_total_usd,
                "max_steps": self._config.max_steps,
            },
        )
        return OrchestrationResult(
            output="", success=False, step_count=self._config.max_steps,
            steps=steps, error=f"Exceeded maximum step count ({self._config.max_steps})",
            tokens_total=tokens_total, cost_usd=cost_total_usd,
        )

    def _execute_tool(self, tc: dict, step_number: int, steps: list[StepRecord]) -> dict:
        logger.info(
            "tool call started",
            extra={
                "event": "tool.call.started",
                "tool.name": tc["name"],
                "step.number": step_number,
            },
        )
        with tracer.trace("agent.tool.call") as tool_span:
            tool_result = self._tools.execute(tc["name"], tc["input"])
            status = "success" if tool_result.success else "error"
            # Stringify numeric values — ddtrace v2 routes int/float through
            # set_metric(), making get_tag() return None for them.
            tool_span.set_tags({
                "tool.name": tc["name"],
                "tool.input_tokens": str(len(str(tc["input"]))),
                "tool.output_tokens": str(len(str(tool_result.output or ""))),
                "tool.latency_ms": str(round(tool_result.latency_ms, 2)),
                "tool.status": status,
            })
            if not tool_result.success:
                # ToolRegistry already caught the exception, so set_traceback()
                # would be a no-op. Set error fields explicitly instead.
                tool_span.error = 1
                tool_span.set_tag("error.message", tool_result.error or "tool execution failed")

        metrics.distribution(
            "agent.tool.call.duration_ms",
            tool_result.latency_ms,
            tags=[f"tool_name:{tc['name']}", f"status:{status}"],
        )
        if not tool_result.success:
            metrics.count(
                "agent.tool.call.error_rate",
                tags=[f"tool_name:{tc['name']}", "error_type:execution_error"],
            )

        event = "tool.call.completed" if tool_result.success else "tool.call.failed"
        log = logger.info if tool_result.success else logger.warning
        log(
            "tool call %s",
            status,
            extra={
                "event": event,
                "tool.name": tc["name"],
                "tool.status": status,
                "tool.latency_ms": round(tool_result.latency_ms, 2),
                "tool.input_size": len(str(tc["input"])),
                "tool.output_size": len(str(tool_result.output or "")),
                "step.number": step_number,
            },
        )

        steps.append(StepRecord(
            step_number=step_number, decision_type="tool_call",
            tool_name=tc["name"],
        ))
        return {
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": str(tool_result.output) if tool_result.success else f"Error: {tool_result.error}",
        }
