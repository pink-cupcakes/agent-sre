"""
LLM client — wraps the Anthropic SDK.

Child spans and token-count attributes are added in initiative 02.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import anthropic

from ..config import Config


@dataclass
class LLMResponse:
    content: str
    model: str
    tokens_prompt: int
    tokens_completion: int
    latency_ms: float
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens"
    tool_calls: list[dict]  # populated when stop_reason == "tool_use"

    @property
    def tokens_total(self) -> int:
        return self.tokens_prompt + self.tokens_completion

    @property
    def is_tool_call(self) -> bool:
        return self.stop_reason == "tool_use" and bool(self.tool_calls)

    @property
    def is_final_answer(self) -> bool:
        return self.stop_reason == "end_turn"


class LLMClient:
    def __init__(self, config: Config) -> None:
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self._model = config.llm_model

    def call(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        start = time.monotonic()

        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        response = self._client.messages.create(**kwargs)
        latency_ms = (time.monotonic() - start) * 1000

        text_content = " ".join(
            block.text
            for block in response.content
            if hasattr(block, "text")
        )

        tool_calls = [
            {
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
            for block in response.content
            if block.type == "tool_use"
        ]

        return LLMResponse(
            content=text_content,
            model=response.model,
            tokens_prompt=response.usage.input_tokens,
            tokens_completion=response.usage.output_tokens,
            latency_ms=latency_ms,
            stop_reason=response.stop_reason,
            tool_calls=tool_calls,
        )
