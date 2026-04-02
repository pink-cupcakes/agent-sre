"""
Tests for initiative 01 — Datadog trace foundation.

Done-when criteria verified here:
  ✓ Datadog tracer initialises on agent startup without errors.
  ✓ A root span is created for every task execution.
  ✓ Root span has all four required tags populated.
"""
from __future__ import annotations

from typing import List, Optional

import pytest
from ddtrace import tracer as dd_tracer
from ddtrace._trace.processor import TraceProcessor
from ddtrace.trace import Span

import agent.observability.tracing as tracing_mod
from agent import Agent, Task
from agent.config import Config
from agent.observability.tracing import Telemetry
from agent.orchestration import OrchestrationResult


# ---------------------------------------------------------------------------
# In-memory processor for test span inspection
# ---------------------------------------------------------------------------


class SpanCapture(TraceProcessor):
    """Captures finished traces in memory instead of forwarding to the agent."""

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
    """Configure app-level telemetry with an in-memory trace processor."""
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


@pytest.fixture
def agent(telemetry, test_config, monkeypatch) -> Agent:
    """Agent with mocked orchestrator — telemetry configured independently."""
    a = Agent.create(config=test_config)

    def mock_run(task):
        return OrchestrationResult(
            output="mocked answer", success=True, step_count=1, tokens_total=100
        )

    monkeypatch.setattr(a._orchestrator, "run", mock_run)
    return a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def finished_root_spans(tel: Telemetry, cap: SpanCapture) -> List[Span]:
    tel.force_flush()
    return [s for s in cap.pop() if s.name == "agent.task"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTelemetryConfiguration:
    def test_configure_does_not_raise(self, test_config, capture):
        t = Telemetry.configure(test_config, trace_processor=capture)
        t.shutdown()

    def test_service_and_env_set_on_configure(self, test_config, capture):
        from ddtrace import config as dd_config
        Telemetry.configure(test_config, trace_processor=capture)
        assert dd_config.service == "agent-sre-test"
        assert dd_config.env == "test"

    def test_processor_captures_spans(self, test_config, capture):
        t = Telemetry.configure(test_config, trace_processor=capture)
        with dd_tracer.trace("test.span"):
            pass
        t.force_flush()
        assert len(capture.pop()) == 1
        t.shutdown()


class TestRootSpan:
    def test_root_span_created(self, agent, telemetry, capture, sample_task):
        agent.run_task(sample_task)
        assert len(finished_root_spans(telemetry, capture)) == 1

    def test_root_span_required_tags(self, agent, telemetry, capture, sample_task):
        agent.run_task(sample_task)
        spans = finished_root_spans(telemetry, capture)
        span = spans[0]
        assert span.get_tag("task.id") == sample_task.task_id
        assert span.get_tag("task.type") == "sre_investigation"
        assert span.get_tag("session.id") == "session-001"
        assert span.get_tag("user.id") == "user-42"

    def test_task_id_is_unique_per_task(self, agent, telemetry, capture):
        for _ in range(2):
            agent.run_task(Task(task_type="t", session_id="s", user_id="u", prompt="x"))
        telemetry.force_flush()
        spans = [s for s in capture.pop() if s.name == "agent.task"]
        assert len({s.get_tag("task.id") for s in spans}) == 2

    def test_one_root_span_per_task(self, agent, telemetry, capture):
        for i in range(5):
            agent.run_task(
                Task(task_type="test", session_id=f"s{i}", user_id=f"u{i}", prompt="p")
            )
        telemetry.force_flush()
        spans = [s for s in capture.pop() if s.name == "agent.task"]
        assert len(spans) == 5

    def test_root_span_error_on_orchestrator_failure(
        self, telemetry, test_config, capture, sample_task, monkeypatch
    ):
        a = Agent.create(config=test_config)
        monkeypatch.setattr(
            a._orchestrator, "run", lambda task: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        result = a.run_task(sample_task)

        assert result.success is False
        spans = finished_root_spans(telemetry, capture)
        assert len(spans) == 1
        assert spans[0].error == 1


class TestMaxSteps:
    def test_max_steps_from_config(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_STEPS", "7")
        from agent.config import load_config
        assert load_config().max_steps == 7

    def test_max_steps_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_MAX_STEPS", raising=False)
        from agent.config import load_config
        assert load_config().max_steps == 20
