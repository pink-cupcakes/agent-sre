# Observability — personal project stack

This directory documents the observability approach for running agent-sre as a
personal / side project. The original plan (`.claude/observability-initiatives/`)
assumed Datadog and AWS. This replaces it with a zero-cost, locally-runnable
stack that can graduate to managed services later without changing instrumentation.

## Current state

| Signal   | Backend               | How to view                          |
|----------|-----------------------|--------------------------------------|
| Logs     | stdout + `agent.log`  | Terminal / `tail -f agent.log \| jq` |
| Metrics  | no-op                 | —                                    |
| Traces   | no-op                 | —                                    |

## Target stack

| Signal   | Backend                      | Infrastructure required              |
|----------|------------------------------|--------------------------------------|
| Logs     | stdout + `agent.log`         | None (already done)                  |
| Metrics  | Prometheus `/metrics` endpoint| None to start; optional Grafana Cloud free tier |
| Traces   | OpenTelemetry → Jaeger       | `docker run jaegertracing/all-in-one` |
| CI/CD    | GitHub Actions summaries     | GitHub (already have access)         |

## Initiatives

```
01-local-tracing-otel-jaeger.md   — Replace no-op tracer with OTEL → Jaeger
02-local-metrics-prometheus.md    — Replace DogStatsd no-op with Prometheus
03-github-actions-observability.md — CI visibility: summaries, benchmarks, dashboards
```

## Constraints

- No Datadog account.
- No AWS account (or treat it as unavailable).
- All backends must run locally with a single Docker command, or require no
  infrastructure at all.
- Instrumentation must stay vendor-neutral (OpenTelemetry) so it can be pointed
  at any backend later (Honeycomb, Grafana Cloud, etc.) by changing an env var.
- Do not log full prompt/response text anywhere persistent. Token counts and
  metadata only.

## Graduation path

When this is no longer a side project:

1. Point the OTEL exporter at Honeycomb or Grafana Tempo (both have generous
   free tiers) by setting `OTEL_EXPORTER_OTLP_ENDPOINT`.
2. Point Prometheus scraping at Grafana Cloud (free tier) instead of local.
3. The instrumentation code changes zero — only env vars change.
