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
from typing import Optional

from ddtrace import config as dd_config
from ddtrace import patch as dd_patch
from ddtrace import tracer

from ..config import Config

logger = logging.getLogger(__name__)


class _SuppressProbes(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/health" not in msg and "/ready" not in msg


def configure_logging(level: int = logging.INFO) -> None:
    """
    Configure root logger to emit structured lines to stdout.
    ddtrace's logging patch injects dd.trace_id/span_id into every record
    so logs correlate with traces in Datadog.
    Probe endpoints are suppressed from uvicorn access logs to reduce noise.
    """
    dd_patch(logging=True)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s "
            "[dd.trace_id=%(dd.trace_id)s dd.span_id=%(dd.span_id)s] "
            "%(message)s"
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(handler)
    logging.getLogger("uvicorn.access").addFilter(_SuppressProbes())


class Telemetry:
    """
    Manages ddtrace lifecycle for the process.

    One instance is created at startup via configure(). Callers never need to
    hold a reference — import tracer directly from ddtrace to create spans.
    """

    @classmethod
    def configure(cls, config: Config, writer=None) -> "Telemetry":
        """
        Set service metadata and register shutdown.

        Pass writer= in tests to capture spans in memory instead of
        forwarding to the Datadog agent.
        """
        configure_logging()
        dd_config.env = config.deployment_env
        dd_config.service = config.service_name

        if writer is not None:
            tracer.configure(writer=writer)

        instance = cls()
        atexit.register(instance.shutdown)

        logger.info(
            "Datadog tracer configured",
            extra={
                "service_name": config.service_name,
                "deployment_env": config.deployment_env,
            },
        )
        return instance

    def force_flush(self) -> None:
        tracer.flush()

    def shutdown(self) -> None:
        tracer.shutdown()
