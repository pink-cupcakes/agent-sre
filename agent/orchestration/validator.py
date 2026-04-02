"""
Response validator — structural guard between every LLM call and the orchestrator.

Checks each LLMResponse against the live ToolRegistry before the orchestrator
acts on it. Entirely deterministic — no extra API calls, no latency impact.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent.llm.client import LLMResponse
from agent.tools import ToolRegistry


@dataclass
class ValidationResult:
    valid: bool
    reason: str | None = None


class ResponseValidator:
    """Validates an LLMResponse against the current ToolRegistry.

    Called once per orchestrator step, immediately after _llm.call() returns
    and before any branching on stop_reason.
    """

    def validate(self, response: LLMResponse, registry: ToolRegistry) -> ValidationResult:
        registered = {d["name"] for d in registry.definitions()}

        # No tools registered: the model must not return any tool calls.
        if not registered and response.tool_calls:
            names = [tc["name"] for tc in response.tool_calls]
            return ValidationResult(
                valid=False,
                reason=(
                    f"Model returned tool calls {names} but no tools are registered. "
                    "This is a hallucinated tool call."
                ),
            )

        # Tools registered: every called tool must exist in the registry.
        if response.is_tool_call:
            unknown = [tc["name"] for tc in response.tool_calls if tc["name"] not in registered]
            if unknown:
                return ValidationResult(
                    valid=False,
                    reason=f"Model called unregistered tools: {unknown}",
                )

        return ValidationResult(valid=True)
