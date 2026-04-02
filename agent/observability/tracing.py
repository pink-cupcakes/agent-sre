"""
Datadog trace foundation.

Call Telemetry.configure() once at startup to set the service name and
deployment environment. Spans are forwarded to the Datadog agent via ddtrace's
default transport — set DD_AGENT_HOST or DD_TRACE_AGENT_URL in the environment
to point at the agent; no API key is needed in the app.

    # startup
    Telemetry.configure(config)

    # anywhere in the codebase
    from ddtrace import tracer
    with tracer.trace("my.operation") as span:
        span.set_tag("key", "value")
"""
from __future__ import annotations

import atexit
import logging
import sys
import traceback

from ddtrace import config as dd_config
from ddtrace import patch as dd_patch
from ddtrace import tracer
from pythonjsonlogger.json import JsonFormatter

from agent.config import Config
from agent.observability import context as ctx

logger = logging.getLogger(__name__)

# Maps logger (module) name → component label for the structured log schema.
_COMPONENT_MAP: dict[str, str] = {
    "agent.agent": "agent",
    "agent.orchestration.orchestrator": "orchestrator",
    "agent.llm.client": "llm",
    "agent.llm.pricing": "llm",
    "agent.tools": "tool",
    "agent.memory.session": "session",
    "agent.memory.store": "memory",
    "agent.observability.tracing": "telemetry",
    "agent.api": "api",
}


def _component_for(name: str) -> str:
    if name in _COMPONENT_MAP:
        return _COMPONENT_MAP[name]
    # best-effort: use the last dotted segment
    return name.rsplit(".", 1)[-1]


class _DatadogJsonFormatter(JsonFormatter):
    def add_fields(
        self,
        log_data: dict,
        record: logging.LogRecord,
        message_dict: dict,
    ) -> None:
        super().add_fields(log_data, record, message_dict)

        # Severity — Datadog uses "status"; spec also names it "level".
        level = record.levelname.lower()
        log_data["status"] = level
        log_data["level"] = level

        # Component inferred from the emitting logger's module path.
        log_data["component"] = _component_for(record.name)

        # Business-level correlation IDs from request-scoped context vars.
        task_id = ctx.task_id_var.get()
        if task_id:
            log_data["task_id"] = task_id
        session_id = ctx.session_id_var.get()
        if session_id:
            log_data["session_id"] = session_id

        if record.exc_info:
            log_data["error.stack"] = "".join(traceback.format_exception(*record.exc_info))
            log_data["error.kind"] = record.exc_info[0].__name__ if record.exc_info[0] else ""
            log_data["error.message"] = str(record.exc_info[1])
            log_data.pop("exc_info", None)


class _SuppressProbes(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/health" not in msg and "/ready" not in msg


def configure_logging(level: int = logging.INFO) -> None:
    """
    Configure root logger to emit one JSON line per record to stdout.
    ddtrace's logging patch injects dd.trace_id/span_id into every record
    so logs correlate with traces in Datadog.
    Probe endpoints are suppressed from uvicorn access logs to reduce noise.
    """
    dd_patch(logging=True)
    formatter = _DatadogJsonFormatter(
        fmt=["message", "name"],  # always present; library calls getMessage() for message
        timestamp=True,           # adds ISO-8601 "timestamp" field
        rename_fields={"name": "logger"},
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(handler)

    # Override uvicorn's own formatter so access logs are also JSON
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [handler]
        uv_logger.propagate = False
    logging.getLogger("uvicorn.access").addFilter(_SuppressProbes())


class Telemetry:
    """
    Manages ddtrace and DogStatsD lifecycle for the process.

    One instance is created at startup via configure(). Callers never need to
    hold a reference — import tracer directly from ddtrace to create spans.
    """

    @classmethod
    def configure(cls, config: Config, trace_processor=None) -> "Telemetry":
        """
        Set service metadata, initialise DogStatsD, and register shutdown.

        Pass trace_processor= in tests to capture spans in memory instead of
        forwarding to the Datadog agent.
        """
        configure_logging()
        dd_config.env = config.deployment_env
        dd_config.service = config.service_name

        if trace_processor is not None:
            tracer.configure(trace_processors=[trace_processor])
            tracer.enabled = True
            tracer.start_span = tracer._start_span

        instance = cls()
        atexit.register(instance.shutdown)

        logger.info(
            "Datadog tracer configured",
            extra={
                "event": "telemetry.configured",
                "service_name": config.service_name,
                "deployment_env": config.deployment_env,
            },
        )
        return instance

    def force_flush(self) -> None:
        tracer.flush()

    def shutdown(self) -> None:
        tracer.shutdown()
