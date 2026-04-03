# 01 — Local tracing: OpenTelemetry → Jaeger

## Goal

Replace the no-op tracer shim with a real OpenTelemetry tracer that exports
spans to Jaeger running locally in Docker. Zero cloud accounts required.

## Why OpenTelemetry

- Vendor-neutral. Changing the backend is one env var (`OTEL_EXPORTER_OTLP_ENDPOINT`).
- Can graduate to Honeycomb, Grafana Tempo, or any OTLP-compatible backend later.
- The existing span structure in `agent.py`, `orchestrator.py`, and `llm/client.py`
  maps directly — no instrumentation changes needed.

## Infrastructure

One Docker command, runs locally:

```bash
docker run -d --name jaeger \
  -p 16686:16686 \
  -p 4317:4317 \
  jaegertracing/all-in-one:latest
```

- `localhost:16686` — Jaeger UI (browser)
- `localhost:4317` — OTLP gRPC endpoint (where the app sends spans)

## Dependencies to add

```
opentelemetry-sdk
opentelemetry-exporter-otlp-proto-grpc
opentelemetry-instrumentation-fastapi   # auto-instruments HTTP routes
opentelemetry-instrumentation-httpx     # auto-instruments outbound HTTP (Anthropic SDK)
```

## What to build

### `agent/observability/tracer.py` — replace the no-op shim

The current shim has the right interface. Replace `_NoOpTracer` with a real
OTEL tracer:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

def configure_tracer(service_name: str, otlp_endpoint: str | None = None) -> None:
    provider = TracerProvider(resource=Resource({"service.name": service_name}))
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
    else:
        exporter = ConsoleSpanExporter()   # fallback: print spans to terminal
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_global_tracer_provider(provider)

tracer = trace.get_tracer(__name__)
```

Key behaviour:
- If `OTEL_EXPORTER_OTLP_ENDPOINT` is set → send to Jaeger (or any OTLP backend).
- If not set → fall back to `ConsoleSpanExporter` which prints spans to stdout.
- If the OTEL SDK isn't installed at all → keep the no-op shim (guard the import
  with try/except).

### `agent/api.py` — call `configure_tracer()` at startup

```python
from agent.observability.tracer import configure_tracer
# in lifespan:
configure_tracer(
    service_name=_config.service_name,
    otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
)
```

### Auto-instrumentation

After calling `configure_tracer()`, add:

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
FastAPIInstrumentor.instrument_app(app)
```

This gives you free HTTP-level spans for every request without touching route code.

## Span inventory (already instrumented)

| Span name          | File              | Tags already set                               |
|--------------------|-------------------|------------------------------------------------|
| `agent.task`       | `agent.py`        | task.id, task.type, session.id, user.id, success, step_count, tokens_total |
| `agent.step`       | `orchestrator.py` | step.number, step.decision_type                |
| `agent.tool.call`  | `orchestrator.py` | tool.name, tool.status, tool.latency_ms        |
| `agent.llm.call`   | `llm/client.py`   | model, tokens_prompt, tokens_completion, latency_ms |

These all use `with tracer.trace("name") as span:` which maps directly to the
OTEL `with tracer.start_as_current_span("name") as span:` API.

> **Note:** The existing code uses `span.set_tag()` (ddtrace API). OTEL uses
> `span.set_attribute()`. The shim or the real tracer needs to handle this
> translation — either add `set_tag` as an alias on the OTEL span wrapper, or
> do a find-replace across the codebase.

## Environment variables

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317   # send to local Jaeger
# or omit it entirely to get ConsoleSpanExporter output
```

## Viewing traces

1. Run Jaeger via Docker (command above).
2. Start the agent: `uvicorn agent.api:app --port 8001`
3. Send a task request.
4. Open `http://localhost:16686` → select service `agent-sre` → find traces.

Each task should produce one root span `agent.task` with child spans for each
step, LLM call, and tool call visible in the waterfall view.

## Done when

- A task request produces a complete trace visible in Jaeger.
- The waterfall shows: `agent.task` → `agent.step` → `agent.llm.call` and
  `agent.tool.call` as children.
- `OTEL_EXPORTER_OTLP_ENDPOINT` unset → spans print to console. Set → spans go to Jaeger.
- FastAPI HTTP spans appear automatically (no code changes needed).
