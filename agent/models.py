"""Shared domain types used across the agent package."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Task:
    """An inbound task for the agent to execute."""

    task_type: str
    session_id: str
    user_id: str
    prompt: str
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class TaskResult:
    task_id: str
    output: str
    success: bool
    step_count: int = 0
    tokens_total: int = 0
    error: Optional[str] = None
