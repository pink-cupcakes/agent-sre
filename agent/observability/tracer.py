"""
No-op tracer shim.

Provides a `tracer` object with the same interface as ddtrace's tracer so
that span instrumentation throughout the codebase compiles and runs without
a Datadog agent. All spans are silently discarded.

Usage (drop-in replacement for `from ddtrace import tracer`):

    from agent.observability.tracer import tracer

    with tracer.trace("my.operation") as span:
        span.set_tag("key", "value")
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator


class _NoOpSpan:
    def set_tag(self, key: str, value: Any) -> None:
        pass

    def set_metric(self, key: str, value: float) -> None:
        pass

    def set_traceback(self) -> None:
        pass

    def finish(self) -> None:
        pass

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _NoOpTracer:
    @contextmanager
    def trace(self, name: str, **kwargs: Any) -> Generator[_NoOpSpan, None, None]:
        yield _NoOpSpan()


tracer = _NoOpTracer()
