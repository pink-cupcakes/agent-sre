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
        session = self._sessions.get_or_create(task.session_id, task.user_id)
        session.add_message("user", task.prompt)

        steps: list[StepRecord] = []
        tokens_total = 0
        cost_total_usd = 0.0

        for step_number in range(1, self._config.max_steps + 1):
            with tracer.trace("agent.step") as step_span:
                step_span.set_tags({"step.number": step_number})

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
                        "agent step %d: llm error",
                        step_number,
                        extra={
                            "step.number": step_number,
                            "step.decision_type": "error",
                            "step.tokens_accumulated": tokens_total,
                        },
                    )
                    steps.append(StepRecord(step_number=step_number, decision_type="error", tool_name=None))
                    return OrchestrationResult(
                        output="", success=False, step_count=step_number,
                        steps=steps, error=str(exc), tokens_total=tokens_total,
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
                        "agent step %d: final_answer tokens=%d cost=$%.6f latency=%.0fms",
                        step_number, llm_response.tokens_total, step_cost, llm_response.latency_ms,
                        extra={
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
                    )

                if llm_response.is_tool_call:
                    first_tool = llm_response.tool_calls[0]["name"] if llm_response.tool_calls else None
                    decision_type = "tool_call"
                    step_span.set_tags({"step.decision_type": decision_type, "step.tool_name": first_tool or ""})
                    logger.info(
                        "agent step %d: tool_call tool=%s tokens=%d cost=$%.6f latency=%.0fms",
                        step_number, first_tool, llm_response.tokens_total, step_cost, llm_response.latency_ms,
                        extra={
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
            "agent exceeded max steps (%d) tokens=%d cost=$%.6f",
            self._config.max_steps, tokens_total, cost_total_usd,
            extra={
                "step.tokens_accumulated": tokens_total,
                "step.cost_accumulated_usd": cost_total_usd,
            },
        )
        return OrchestrationResult(
            output="", success=False, step_count=self._config.max_steps,
            steps=steps, error=f"Exceeded maximum step count ({self._config.max_steps})",
            tokens_total=tokens_total,
        )

    def _execute_tool(self, tc: dict, step_number: int, steps: list[StepRecord]) -> dict:
        with tracer.trace("agent.tool.call") as tool_span:
            tool_result = self._tools.execute(tc["name"], tc["input"])
            status = "success" if tool_result.success else "error"
            tool_span.set_tags({
                "tool.name": tc["name"],
                "tool.input_tokens": len(str(tc["input"])),
                "tool.output_tokens": len(str(tool_result.output or "")),
                "tool.latency_ms": tool_result.latency_ms,
                "tool.status": status,
            })
            if not tool_result.success:
                tool_span.set_traceback()

        log = logger.info if tool_result.success else logger.warning
        log(
            "tool call: %s status=%s latency=%.0fms",
            tc["name"], status, tool_result.latency_ms,
            extra={
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
