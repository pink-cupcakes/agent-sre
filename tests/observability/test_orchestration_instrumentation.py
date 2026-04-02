"""
Tests for initiative 02 — orchestration, LLM, and tool call spans.

Done-when criteria verified here:
  ✓ Every task trace shows root → agent.step → agent.llm.call hierarchy.
  ✓ Step spans have step.number, step.decision_type, step.tool_name attributes.
  ✓ LLM spans have model, token counts, latency, and streaming attributes.
  ✓ Tool call spans have name, input/output tokens, latency, and status attributes.
  ✓ Errors set span.error = 1 with descriptive tags.
  ✓ Multiple concurrent tool calls each get their own span with correct parent.
"""
from __future__ import annotations

from typing import List, Optional
from unittest.mock import MagicMock

import pytest
from ddtrace._trace.processor import TraceProcessor
from ddtrace.trace import Span

from agent import Agent, Task
from agent.config import Config
from agent.llm.client import LLMResponse
from agent.observability.tracing import Telemetry
from agent.tools import ToolDefinition


# ---------------------------------------------------------------------------
# In-memory processor for test span inspection
# ---------------------------------------------------------------------------


class SpanCapture(TraceProcessor):
    def __init__(self) -> None:
        self._spans: List[Span] = []

    def process_trace(self, trace: List[Span]) -> Optional[List[Span]]:
        self._spans.extend(trace)
        return trace

    def pop(self) -> List[Span]:
        spans = self._spans[:]
        self._spans.clear()
        return spans


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def capture() -> SpanCapture:
    return SpanCapture()


@pytest.fixture
def test_config() -> Config:
    return Config(
        service_name="agent-sre-test",
        deployment_env="test",
        anthropic_api_key="sk-test",
        llm_model="claude-sonnet-4-20250514",
        max_steps=5,
    )


@pytest.fixture
def telemetry(test_config, capture) -> Telemetry:
    t = Telemetry.configure(test_config, trace_processor=capture)
    yield t
    t.shutdown()


@pytest.fixture
def sample_task() -> Task:
    return Task(
        task_type="sre_investigation",
        session_id="session-001",
        user_id="user-42",
        prompt="Why is p99 latency spiking?",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(
    content: str = "done",
    stop_reason: str = "end_turn",
    tool_calls: list | None = None,
    input_tokens: int = 50,
    output_tokens: int = 20,
    model: str = "claude-sonnet-4-20250514",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model=model,
        tokens_prompt=input_tokens,
        tokens_completion=output_tokens,
        latency_ms=123.4,
        stop_reason=stop_reason,
        tool_calls=tool_calls or [],
    )


def _stub_anthropic_response(agent: Agent, llm_response: LLMResponse) -> None:
    """Patch client.messages.create to return a fake Anthropic response object
    so the LLMClient span logic (wrapping the real call) still executes."""
    fake = MagicMock()
    fake.model = llm_response.model
    fake.stop_reason = llm_response.stop_reason
    fake.usage.input_tokens = llm_response.tokens_prompt
    fake.usage.output_tokens = llm_response.tokens_completion
    text_block = MagicMock()
    text_block.text = llm_response.content
    text_block.type = "text"
    fake.content = [text_block]
    agent._orchestrator._llm._client.messages.create = MagicMock(return_value=fake)


def _mock_final(agent: Agent, *, via_client: bool = False) -> None:
    """Stub LLM to return a single final-answer response."""
    response = _make_llm_response(stop_reason="end_turn", content="done")
    if via_client:
        _stub_anthropic_response(agent, response)
    else:
        agent._orchestrator._llm.call = MagicMock(return_value=response)


def _mock_one_tool_then_final(agent: Agent) -> None:
    tool_resp = _make_llm_response(
        stop_reason="tool_use",
        tool_calls=[{"id": "t1", "name": "my_tool", "input": {"query": "hello"}}],
    )
    final_resp = _make_llm_response(stop_reason="end_turn", content="done")
    call_count = [0]

    def _call(**kwargs):
        call_count[0] += 1
        return tool_resp if call_count[0] == 1 else final_resp

    agent._orchestrator._llm.call = _call
    agent._orchestrator._tools.register(
        ToolDefinition(name="my_tool", description="test", input_schema={}, handler=lambda query: "result")
    )


def _mock_failing_tool_then_final(agent: Agent) -> None:
    tool_resp = _make_llm_response(
        stop_reason="tool_use",
        tool_calls=[{"id": "t1", "name": "broken_tool", "input": {}}],
    )
    final_resp = _make_llm_response(stop_reason="end_turn", content="done")
    call_count = [0]

    def _call(**kwargs):
        call_count[0] += 1
        return tool_resp if call_count[0] == 1 else final_resp

    agent._orchestrator._llm.call = _call

    def _broken():
        raise RuntimeError("tool failed")

    agent._orchestrator._tools.register(
        ToolDefinition(name="broken_tool", description="always fails", input_schema={}, handler=_broken)
    )


def _mock_two_tools_then_final(agent: Agent) -> None:
    tool_resp = _make_llm_response(
        stop_reason="tool_use",
        tool_calls=[
            {"id": "t1", "name": "tool_a", "input": {"x": 1}},
            {"id": "t2", "name": "tool_b", "input": {"y": 2}},
        ],
    )
    final_resp = _make_llm_response(stop_reason="end_turn", content="done")
    call_count = [0]

    def _call(**kwargs):
        call_count[0] += 1
        return tool_resp if call_count[0] == 1 else final_resp

    agent._orchestrator._llm.call = _call
    for name in ("tool_a", "tool_b"):
        agent._orchestrator._tools.register(
            ToolDefinition(name=name, description="test", input_schema={}, handler=lambda **kw: "ok")
        )


def _flush(tel: Telemetry, cap: SpanCapture) -> List[Span]:
    tel.force_flush()
    return cap.pop()


# ---------------------------------------------------------------------------
# Step span tests
# ---------------------------------------------------------------------------


class TestStepSpans:
    def test_step_span_created_for_each_iteration(self, telemetry, test_config, capture, sample_task):
        """Two tool-call steps + one final-answer step → three step spans."""
        tool_resp = _make_llm_response(
            stop_reason="tool_use",
            tool_calls=[{"id": "t1", "name": "my_tool", "input": {"k": "v"}}],
        )
        final_resp = _make_llm_response(stop_reason="end_turn", content="done")
        call_count = [0]

        def _call(**kwargs):
            call_count[0] += 1
            return tool_resp if call_count[0] <= 2 else final_resp

        a = Agent.create(config=test_config)
        a._orchestrator._llm.call = _call
        a._orchestrator._tools.register(
            ToolDefinition(name="my_tool", description="", input_schema={}, handler=lambda k: "ok")
        )
        a.run_task(sample_task)

        step_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.step"]
        assert len(step_spans) == 3

    def test_step_number_tag(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_final(a)
        a.run_task(sample_task)

        step_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.step"]
        assert len(step_spans) == 1
        assert step_spans[0].get_tag("step.number") == "1"

    def test_step_decision_type_final_answer(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_final(a)
        a.run_task(sample_task)

        step_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.step"]
        assert step_spans[0].get_tag("step.decision_type") == "final_answer"

    def test_step_decision_type_and_tool_name(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_one_tool_then_final(a)
        a.run_task(sample_task)

        step_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.step"]
        tool_step = next(s for s in step_spans if s.get_tag("step.decision_type") == "tool_call")
        assert tool_step.get_tag("step.tool_name") == "my_tool"

    def test_step_error_on_llm_failure(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        a._orchestrator._llm.call = MagicMock(side_effect=RuntimeError("LLM down"))
        a.run_task(sample_task)

        step_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.step"]
        assert len(step_spans) == 1
        assert step_spans[0].get_tag("step.decision_type") == "error"
        assert step_spans[0].error == 1

    def test_step_span_is_child_of_root(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_final(a)
        a.run_task(sample_task)

        spans = _flush(telemetry, capture)
        root = next(s for s in spans if s.name == "agent.task")
        step = next(s for s in spans if s.name == "agent.step")
        assert step.parent_id == root.span_id


# ---------------------------------------------------------------------------
# LLM call span tests
# ---------------------------------------------------------------------------


class TestLLMCallSpans:
    def test_llm_span_created(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_final(a, via_client=True)
        a.run_task(sample_task)

        llm_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.llm.call"]
        assert len(llm_spans) == 1

    def test_llm_span_attributes(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_final(a, via_client=True)
        a.run_task(sample_task)

        span = next(s for s in _flush(telemetry, capture) if s.name == "agent.llm.call")
        assert span.get_tag("llm.model") == "claude-sonnet-4-20250514"
        assert span.get_tag("llm.tokens.prompt") == "50"
        assert span.get_tag("llm.tokens.completion") == "20"
        assert span.get_tag("llm.tokens.total") == "70"
        assert span.get_tag("llm.streaming") == "False"
        assert float(span.get_tag("llm.latency_ms")) > 0

    def test_llm_span_is_child_of_step(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_final(a, via_client=True)
        a.run_task(sample_task)

        spans = _flush(telemetry, capture)
        step_span = next(s for s in spans if s.name == "agent.step")
        llm_span = next(s for s in spans if s.name == "agent.llm.call")
        assert llm_span.parent_id == step_span.span_id

    def test_llm_timeout_sets_error_type(self, telemetry, test_config, capture, sample_task):
        import anthropic

        a = Agent.create(config=test_config)
        a._orchestrator._llm._client.messages.create = MagicMock(
            side_effect=anthropic.APITimeoutError(request=MagicMock())
        )
        a.run_task(sample_task)

        llm_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.llm.call"]
        assert len(llm_spans) == 1
        assert llm_spans[0].error == 1
        assert llm_spans[0].get_tag("llm.error.type") == "timeout"

    def test_llm_rate_limit_sets_error_type(self, telemetry, test_config, capture, sample_task):
        import anthropic

        mock_response = MagicMock()
        mock_response.headers = {}
        a = Agent.create(config=test_config)
        a._orchestrator._llm._client.messages.create = MagicMock(
            side_effect=anthropic.RateLimitError(message="rate limited", response=mock_response, body={})
        )
        a.run_task(sample_task)

        llm_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.llm.call"]
        assert llm_spans[0].error == 1
        assert llm_spans[0].get_tag("llm.error.type") == "rate_limit"

    def test_llm_api_error_sets_malformed_response(self, telemetry, test_config, capture, sample_task):
        import anthropic

        a = Agent.create(config=test_config)
        a._orchestrator._llm._client.messages.create = MagicMock(
            side_effect=anthropic.BadRequestError(
                message="bad request", response=MagicMock(status_code=400), body={}
            )
        )
        a.run_task(sample_task)

        llm_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.llm.call"]
        assert llm_spans[0].error == 1
        assert llm_spans[0].get_tag("llm.error.type") == "malformed_response"


# ---------------------------------------------------------------------------
# Tool call span tests
# ---------------------------------------------------------------------------


class TestToolCallSpans:
    def test_tool_span_created(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_one_tool_then_final(a)
        a.run_task(sample_task)

        tool_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.tool.call"]
        assert len(tool_spans) == 1

    def test_tool_span_attributes_success(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_one_tool_then_final(a)
        a.run_task(sample_task)

        span = next(s for s in _flush(telemetry, capture) if s.name == "agent.tool.call")
        assert span.get_tag("tool.name") == "my_tool"
        assert span.get_tag("tool.status") == "success"
        assert int(span.get_tag("tool.input_tokens")) > 0
        assert float(span.get_tag("tool.latency_ms")) >= 0

    def test_tool_span_is_child_of_step(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_one_tool_then_final(a)
        a.run_task(sample_task)

        spans = _flush(telemetry, capture)
        tool_step = next(
            s for s in spans
            if s.name == "agent.step" and s.get_tag("step.decision_type") == "tool_call"
        )
        tool_span = next(s for s in spans if s.name == "agent.tool.call")
        assert tool_span.parent_id == tool_step.span_id

    def test_tool_error_sets_span_error(self, telemetry, test_config, capture, sample_task):
        a = Agent.create(config=test_config)
        _mock_failing_tool_then_final(a)
        a.run_task(sample_task)

        tool_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.tool.call"]
        assert len(tool_spans) == 1
        assert tool_spans[0].error == 1
        assert tool_spans[0].get_tag("tool.status") == "error"
        assert tool_spans[0].get_tag("error.message") != ""

    def test_multiple_tool_calls_each_get_own_span(self, telemetry, test_config, capture, sample_task):
        """A single LLM response with two tool_use blocks → two tool spans."""
        a = Agent.create(config=test_config)
        _mock_two_tools_then_final(a)
        a.run_task(sample_task)

        tool_spans = [s for s in _flush(telemetry, capture) if s.name == "agent.tool.call"]
        assert len(tool_spans) == 2
        assert {s.get_tag("tool.name") for s in tool_spans} == {"tool_a", "tool_b"}

    def test_multiple_tool_spans_same_parent_step(self, telemetry, test_config, capture, sample_task):
        """Both tool spans from the same iteration share the same parent step span."""
        a = Agent.create(config=test_config)
        _mock_two_tools_then_final(a)
        a.run_task(sample_task)

        spans = _flush(telemetry, capture)
        tool_step = next(
            s for s in spans
            if s.name == "agent.step" and s.get_tag("step.decision_type") == "tool_call"
        )
        tool_spans = [s for s in spans if s.name == "agent.tool.call"]
        assert all(s.parent_id == tool_step.span_id for s in tool_spans)
