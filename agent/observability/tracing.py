"""
Logging setup.

Call configure_logging() (or Telemetry.configure()) once at startup.

Two handlers are installed:
  - stdout  — human-readable plain text, for watching the terminal
  - agent.log — one JSON object per line, for post-hoc inspection / grep

    # startup
    from agent.observability.tracing import configure_logging
    configure_logging()

    # anywhere in the codebase
    import logging
    logger = logging.getLogger(__name__)
    logger.info("something happened", extra={"key": "value"})
"""
from __future__ import annotations

import logging
import sys
import traceback

from pythonjsonlogger.json import JsonFormatter

from agent.config import Config
from agent.observability import context as ctx

logger = logging.getLogger(__name__)

_LOG_FILE = "agent.log"

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
    return name.rsplit(".", 1)[-1]


class _JsonFormatter(JsonFormatter):
    def add_fields(
        self,
        log_data: dict,
        record: logging.LogRecord,
        message_dict: dict,
    ) -> None:
        super().add_fields(log_data, record, message_dict)

        level = record.levelname.lower()
        log_data["status"] = level
        log_data["level"] = level
        log_data["component"] = _component_for(record.name)

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
    Install two log handlers on the root logger:
      - Plain text to stdout (human-readable terminal output)
      - JSON to agent.log (structured, one object per line)

    Probe endpoints are suppressed from uvicorn access logs to reduce noise.
    """
    root = logging.getLogger()
    root.setLevel(level)

    if root.handlers:
        return

    # --- plain text → stdout ---
    text_handler = logging.StreamHandler(sys.stdout)
    text_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # --- JSON → agent.log ---
    file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(
        _JsonFormatter(
            fmt=["message", "name"],
            timestamp=True,
            rename_fields={"name": "logger"},
        )
    )

    root.addHandler(text_handler)
    root.addHandler(file_handler)

    # Override uvicorn's own formatter so access logs are also captured
    probe_filter = _SuppressProbes()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [text_handler, file_handler]
        uv_logger.propagate = False
    logging.getLogger("uvicorn.access").addFilter(probe_filter)


class Telemetry:
    """
    Thin wrapper kept for API compatibility with existing startup code.
    Calls configure_logging() and returns an instance with no-op flush/shutdown.
    """

    @classmethod
    def configure(cls, config: Config, trace_processor=None) -> "Telemetry":
        configure_logging()
        instance = cls()
        logger.info(
            "Logging configured",
            extra={
                "event": "telemetry.configured",
                "service_name": config.service_name,
                "deployment_env": config.deployment_env,
            },
        )
        return instance

    def force_flush(self) -> None:
        pass

    def shutdown(self) -> None:
        pass
