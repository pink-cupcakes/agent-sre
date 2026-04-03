"""
LLM client — wraps the Anthropic SDK.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import anthropic
from agent.observability.tracer import tracer

from agent.config import Config
from agent.llm.pricing import get_cost_usd
from agent.observability import metrics

logger = logging.getLogger(__name__)


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

            logger.info(
                "llm call started",
                extra={
                    "event": "llm.call.started",
                    "llm.model": self._model,
                },
            )

            start = time.monotonic()
            try:
                response = self._client.messages.create(**kwargs)
            except anthropic.APITimeoutError:
                span.error = 1
                span.set_tag("llm.error.type", "timeout")
                logger.warning(
                    "llm call failed: timeout",
                    extra={"event": "llm.call.failed", "llm.error.type": "timeout"},
                )
                raise
            except anthropic.RateLimitError as exc:
                span.error = 1
                span.set_tag("llm.error.type", "rate_limit")
                retry_after = None
                try:
                    retry_after = exc.response.headers.get("retry-after")
                    if retry_after:
                        span.set_tag("llm.error.retry_after", retry_after)
                except Exception:
                    pass
                logger.warning(
                    "llm call failed: rate limit",
                    extra={
                        "event": "llm.call.failed",
                        "llm.error.type": "rate_limit",
                        "llm.error.retry_after": retry_after,
                    },
                )
                raise
            except anthropic.APIError:
                span.error = 1
                span.set_tag("llm.error.type", "malformed_response")
                logger.warning(
                    "llm call failed: api error",
                    extra={"event": "llm.call.failed", "llm.error.type": "malformed_response"},
                )
                raise

            latency_ms = (time.monotonic() - start) * 1000
            cost_usd = get_cost_usd(
                response.model,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )

            # Span tags — token counts for APM flame graphs / dashboards.
            # Tag values that are numeric are stored via set_metric() in ddtrace v2,
            # making get_tag() return None. Stringify them so get_tag() works
            # and downstream consumers can read them as strings.
            span.set_tags({
                "llm.model": response.model,
                "llm.stop_reason": str(response.stop_reason),
                "llm.tokens.prompt": str(response.usage.input_tokens),
                "llm.tokens.completion": str(response.usage.output_tokens),
                "llm.tokens.total": str(response.usage.input_tokens + response.usage.output_tokens),
                "llm.tokens.cache_read": str(response.usage.cache_read_input_tokens or 0),
                "llm.tokens.cache_creation": str(response.usage.cache_creation_input_tokens or 0),
                "llm.latency_ms": str(round(latency_ms, 2)),
                "llm.cost_usd": str(cost_usd),
            })

            # DogStatsD metrics — non-blocking UDP, no hot-path impact.
            model_tag = f"model:{response.model}"
            metrics.distribution("agent.llm.call.duration_ms", latency_ms, tags=[model_tag, "status:success"])
            metrics.distribution("agent.llm.call.tokens.prompt", response.usage.input_tokens, tags=[model_tag])
            metrics.distribution("agent.llm.call.tokens.completion", response.usage.output_tokens, tags=[model_tag])

            # Structured log — full Anthropic response as nested JSON so every
            # field is traversable in Datadog (anthropic.usage.input_tokens, etc.).
            logger.info(
                "llm call completed",
                extra={
                    "event": "llm.call.completed",
                    "llm.model": response.model,
                    "llm.stop_reason": str(response.stop_reason),
                    "llm.latency_ms": round(latency_ms, 2),
                    "llm.cost_usd": cost_usd,
                    "anthropic": response.model_dump(exclude_none=True),
                },
            )

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
