# 02 — Local metrics: Prometheus

## Goal

Replace the DogStatsd no-op (`agent/observability/metrics.py`) with a
Prometheus client that exposes a `/metrics` endpoint on the FastAPI app.
No agent, no sidecar, no cloud account required to start.

## Why Prometheus

- `prometheus_client` is a single pip dependency with no infrastructure.
- Metrics are viewable immediately by curling `localhost:8001/metrics`.
- Can connect Grafana Cloud free tier (remote_write) or run Grafana locally
  via Docker for dashboards — without changing any instrumentation code.

## Dependencies to add

```
prometheus-client
```

## What to build

### `agent/observability/metrics.py` — rewrite around prometheus_client

Replace the DogStatsd calls with Prometheus metric types. The public API
(`distribution`, `count`, `gauge`) stays identical so no call-sites change.

```python
from prometheus_client import Counter, Gauge, Histogram

# Declare metrics at module level (Prometheus requires this)
_task_duration   = Histogram("agent_task_duration_ms", "Task duration", ["task_type", "status"])
_task_step_count = Histogram("agent_task_step_count", "Steps per task", ["task_type"])
_task_tokens     = Histogram("agent_task_tokens_total", "Tokens per task", ["task_type", "model"])
_task_cost       = Histogram("agent_task_cost_usd", "Cost per task", ["task_type", "model"])
_tool_duration   = Histogram("agent_tool_duration_ms", "Tool call duration", ["tool_name", "status"])
_tool_errors     = Counter("agent_tool_errors_total", "Tool errors", ["tool_name"])
_active_sessions = Gauge("agent_sessions_active", "Active sessions")
```

Use labelled `Histogram.labels(...).observe(value)` rather than the generic
`distribution()` shim — this gives proper Prometheus label cardinality and
enables percentile queries (`histogram_quantile`).

Alternatively, keep the existing generic shim API for minimal diff:

```python
_histograms: dict[str, Histogram] = {}
_counters: dict[str, Counter] = {}
_gauges: dict[str, Gauge] = {}

def distribution(metric: str, value: float, tags: list[str] | None = None) -> None:
    label_names, label_values = _parse_tags(tags)
    key = f"{metric}:{','.join(label_names)}"
    if key not in _histograms:
        _histograms[key] = Histogram(metric.replace(".", "_"), metric, label_names)
    _histograms[key].labels(*label_values).observe(value)
```

> The generic shim approach is simpler to implement but produces less idiomatic
> Prometheus metric names. For a POC, it's fine.

### `agent/api.py` — mount the `/metrics` endpoint

```python
from prometheus_client import make_asgi_app

# In lifespan or at app creation:
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)
```

That's it — no other changes to `api.py`.

### Remove `metrics.init()` call

The Prometheus client doesn't need an `init()`. Remove the `metrics.init(...)`
call from the `lifespan` function in `api.py` (or make it a no-op).

## Metric inventory (already instrumented)

All of these are already called in `agent.py` and `orchestrator.py`:

| Metric                         | Type         | Labels                          |
|--------------------------------|--------------|---------------------------------|
| `agent.task.duration_ms`       | Histogram    | task_type, status               |
| `agent.task.step_count`        | Histogram    | task_type                       |
| `agent.task.tokens.total`      | Histogram    | task_type, model                |
| `agent.task.cost_usd`          | Histogram    | task_type, model                |
| `agent.tool.call.duration_ms`  | Histogram    | tool_name, status               |
| `agent.tool.call.error_rate`   | Counter      | tool_name                       |
| `agent.session.active`         | Gauge        | —                               |

## Viewing metrics

### Option A — curl (zero infrastructure)

```bash
curl http://localhost:8001/metrics
```

Output is plain text Prometheus exposition format. Readable enough for spot-checks.

### Option B — Grafana Cloud free tier (recommended)

1. Sign up at grafana.com (free, no credit card).
2. Install the Grafana Agent locally (one binary, no Docker needed).
3. Configure it to scrape `localhost:8001/metrics` and remote_write to your
   Grafana Cloud stack.
4. Import a dashboard — or build one with the queries below.

### Option C — local Grafana via Docker

```bash
docker run -d --name grafana -p 3000:3000 grafana/grafana
```

Then add a Prometheus data source pointing at `host.docker.internal:8001/metrics`
(note: Prometheus itself is not needed — Grafana can query the metrics endpoint
directly via its built-in Prometheus data source if you also run a local
Prometheus scraper, or use the Infinity plugin to hit the endpoint directly).

Actually easier with Prometheus:

```bash
docker run -d --name prometheus \
  -p 9090:9090 \
  -v $(pwd)/prometheus.yml:/etc/prometheus/prometheus.yml \
  prom/prometheus
```

`prometheus.yml`:
```yaml
scrape_configs:
  - job_name: agent-sre
    static_configs:
      - targets: ['host.docker.internal:8001']
```

## Useful Prometheus queries

```promql
# P99 task latency
histogram_quantile(0.99, rate(agent_task_duration_ms_bucket[5m]))

# Task success rate
rate(agent_task_duration_ms_count{status="success"}[5m])
  / rate(agent_task_duration_ms_count[5m])

# Cumulative cost
sum(agent_task_cost_usd_sum)

# Tool error rate
rate(agent_tool_errors_total[5m])

# Average steps per task
rate(agent_task_step_count_sum[5m]) / rate(agent_task_step_count_count[5m])
```

## Done when

- `GET /metrics` returns Prometheus exposition format with the metric inventory above.
- After running a task, `agent_task_duration_ms_count` increments by 1.
- Optional: metrics visible in Grafana Cloud or local Grafana with a basic dashboard.
