"""
Tool registry.

Register callable tools here. The orchestrator looks up tools by name and
calls execute(). Each tool definition also carries the JSON schema that gets
sent to the LLM so it knows how to invoke the tool.

Child spans for tool calls are added in initiative 02.
Concrete tool implementations live as sibling modules in this package.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]


@dataclass
class ToolResult:
    tool_name: str
    output: Any
    success: bool
    error: str | None = None
    latency_ms: float = 0.0


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def definitions(self) -> list[dict]:
        """Return tool schemas in Anthropic tool-use format."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def execute(self, name: str, args: dict) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                tool_name=name,
                output=None,
                success=False,
                error=f"Unknown tool: {name}",
            )

        start = time.monotonic()
        try:
            output = tool.handler(**args)
            latency_ms = (time.monotonic() - start) * 1000
            return ToolResult(tool_name=name, output=output, success=True, latency_ms=latency_ms)
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            return ToolResult(
                tool_name=name,
                output=None,
                success=False,
                error=str(exc),
                latency_ms=latency_ms,
            )


def default_tools() -> ToolRegistry:
    """Return a registry pre-loaded with the built-in tools."""
    return ToolRegistry()
