"""
Request-scoped context variables.

Set task_id_var and session_id_var at the start of each task so that every
log line emitted during that task automatically carries those IDs — without
having to thread them through every function signature.
"""
from __future__ import annotations

from contextvars import ContextVar

task_id_var: ContextVar[str] = ContextVar("agent.task_id", default="")
session_id_var: ContextVar[str] = ContextVar("agent.session_id", default="")
