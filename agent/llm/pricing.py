"""
LLM cost estimation.

Anthropic's API returns token counts but no pricing. Cost must be computed
client-side from tokens × price-per-token — this is the same approach used
by LangSmith, Helicone, and every other LLM observability platform.

Source of truth: litellm's community pricing JSON, which is updated on every
litellm release when Anthropic (or any provider) changes their rates:
  https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json

The in-process cache refreshes at most once per day. If the fetch fails,
the previous cache is kept; if there is no cache yet (first startup, no
network), the hardcoded fallback table is used so cost is always estimated.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main"
    "/model_prices_and_context_window.json"
)
_TTL_SECONDS = 86_400  # refresh once per day
_FETCH_TIMEOUT_S = 5

# Per-token prices (USD) kept in sync with Anthropic's public pricing page.
# Used when the live fetch fails or the model isn't in the remote table.
# Format: "model-prefix": (input_per_token, output_per_token)
_FALLBACK: dict[str, tuple[float, float]] = {
    "claude-opus-4":     (15.0 / 1_000_000,  75.0 / 1_000_000),
    "claude-3-opus":     (15.0 / 1_000_000,  75.0 / 1_000_000),
    "claude-sonnet-4":   (3.0  / 1_000_000,  15.0 / 1_000_000),
    "claude-3-5-sonnet": (3.0  / 1_000_000,  15.0 / 1_000_000),
    "claude-3-5-haiku":  (0.80 / 1_000_000,   4.0 / 1_000_000),
    "claude-3-haiku":    (0.25 / 1_000_000,   1.25 / 1_000_000),
}

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_cache_fetched_at: float = 0.0


def _fetch() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(_PRICING_URL, timeout=_FETCH_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning("llm pricing fetch failed, keeping cached/fallback values: %s", exc)
        return {}


def _get_pricing() -> dict[str, Any]:
    global _cache, _cache_fetched_at
    now = time.monotonic()
    if now - _cache_fetched_at <= _TTL_SECONDS:
        return _cache
    with _lock:
        if now - _cache_fetched_at > _TTL_SECONDS:
            fresh = _fetch()
            if fresh:
                _cache = fresh
                logger.info("refreshed llm pricing cache (%d models)", len(_cache))
            _cache_fetched_at = now  # always bump to avoid hammering on failure
    return _cache


def get_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Return estimated cost in USD for the given model and token counts.

    Token counts come directly from Anthropic's usage response. Prices are
    fetched from litellm's community pricing database (refreshed daily) with
    a hardcoded fallback table for offline / unknown-model cases.
    """
    entry = _get_pricing().get(model)
    if entry:
        in_price = entry.get("input_cost_per_token", 0.0)
        out_price = entry.get("output_cost_per_token", 0.0)
        return round(input_tokens * in_price + output_tokens * out_price, 6)

    # Prefix-match fallback handles snapshot suffixes like
    # "claude-sonnet-4-20250514" → "claude-sonnet-4"
    for prefix, (in_price, out_price) in _FALLBACK.items():
        if prefix in model:
            return round(input_tokens * in_price + output_tokens * out_price, 6)

    logger.warning("no pricing data for model %r, reporting cost_usd=0", model)
    return 0.0
