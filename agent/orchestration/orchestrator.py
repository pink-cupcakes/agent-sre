"""
Orchestrator — drives the decide → act → observe loop.

Child spans for loop iterations, LLM calls, and tool calls are added in
initiative 02.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import Config
from ..llm import LLMClient, LLMResponse
from ..memory.session import SessionStore
from ..memory.store import MemoryStore
from ..models import Task
from ..tools import ToolRegistry

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

        for step_number in range(1, self._config.max_steps + 1):
            logger.info("orchestrator step %d", step_number)

            try:
                llm_response = self._llm.call(
                    messages=session.messages,
                    system=self.SYSTEM_PROMPT,
                    tools=self._tools.definitions() or None,
                )
            except Exception as exc:
                logger.exception("LLM call failed on step %d", step_number)
                steps.append(
                    StepRecord(step_number=step_number, decision_type="error", tool_name=None)
                )
                return OrchestrationResult(
                    output="",
                    success=False,
                    step_count=step_number,
                    steps=steps,
                    error=str(exc),
                    tokens_total=tokens_total,
                )

            tokens_total += llm_response.tokens_total

            if llm_response.is_final_answer:
                steps.append(
                    StepRecord(
                        step_number=step_number,
                        decision_type="final_answer",
                        tool_name=None,
                        llm_response=llm_response,
                    )
                )
                session.add_message("assistant", llm_response.content)
                self._sessions.save(session)
                return OrchestrationResult(
                    output=llm_response.content,
                    success=True,
                    step_count=step_number,
                    steps=steps,
                    tokens_total=tokens_total,
                )

            if llm_response.is_tool_call:
                tool_results_for_llm: list[dict] = []

                for tc in llm_response.tool_calls:
                    tool_result = self._tools.execute(tc["name"], tc["input"])
                    steps.append(
                        StepRecord(
                            step_number=step_number,
                            decision_type="tool_call",
                            tool_name=tc["name"],
                            llm_response=llm_response,
                        )
                    )
                    tool_results_for_llm.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": str(tool_result.output)
                            if tool_result.success
                            else f"Error: {tool_result.error}",
                        }
                    )

                session.add_message(
                    "assistant",
                    [
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["input"],
                        }
                        for tc in llm_response.tool_calls
                    ],
                )
                session.add_message("user", tool_results_for_llm)
                continue

        return OrchestrationResult(
            output="",
            success=False,
            step_count=self._config.max_steps,
            steps=steps,
            error=f"Exceeded maximum step count ({self._config.max_steps})",
            tokens_total=tokens_total,
        )
