"""Runtime configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    service_name: str
    deployment_env: str
    # LLM
    anthropic_api_key: str
    llm_model: str
    # Orchestration
    max_steps: int


def load_config() -> Config:
    return Config(
        service_name=os.getenv("SERVICE_NAME", "agent-sre"),
        deployment_env=os.getenv("DEPLOYMENT_ENV", "development"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", "claude-sonnet-4-20250514"),
        max_steps=int(os.getenv("AGENT_MAX_STEPS", "20")),
    )
