"""
LLM client — wraps the Anthropic SDK.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import anthropic
from ddtrace import tracer

from agent.config import Config


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
        with tracer.trace("agent.llm.call") as span:
            span.set_tag("llm.model", self._model)
            span.set_tag("llm.streaming", False)

            kwargs: dict = {
                "model": self._model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            if tools:
                kwargs["tools"] = tools

            start = time.monotonic()
            try:
                response = self._client.messages.create(**kwargs)
            except anthropic.APITimeoutError:
                span.error = 1
                span.set_tag("llm.error.type", "timeout")
                raise
            except anthropic.RateLimitError as exc:
                span.error = 1
                span.set_tag("llm.error.type", "rate_limit")
                try:
                    retry_after = exc.response.headers.get("retry-after")
                    if retry_after:
                        span.set_tag("llm.error.retry_after", retry_after)
                except Exception:
                    pass
                raise
            except anthropic.APIError:
                span.error = 1
                span.set_tag("llm.error.type", "malformed_response")
                raise

            latency_ms = (time.monotonic() - start) * 1000

            span.set_tag("llm.tokens.prompt", str(response.usage.input_tokens))
            span.set_tag("llm.tokens.completion", str(response.usage.output_tokens))
            span.set_tag("llm.tokens.total", str(response.usage.input_tokens + response.usage.output_tokens))
            span.set_tag("llm.latency_ms", str(latency_ms))

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
