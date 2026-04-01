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
from typing import Optional

from ddtrace import config as dd_config
from ddtrace import tracer

from ..config import Config

logger = logging.getLogger(__name__)


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
