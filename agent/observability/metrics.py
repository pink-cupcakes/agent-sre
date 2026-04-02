from __future__ import annotations

from typing import Sequence

from datadog import DogStatsd  # type: ignore[import-untyped]

_client: DogStatsd | None = None


def init(host: str = "localhost", port: int = 8125) -> None:
    global _client
    _client = DogStatsd(host=host, port=port)


def distribution(metric: str, value: float, tags: Sequence[str] | None = None) -> None:
    if _client is not None:
        _client.distribution(metric, value, tags=list(tags) if tags else [])


def count(metric: str, value: int = 1, tags: Sequence[str] | None = None) -> None:
    if _client is not None:
        _client.increment(metric, value=value, tags=list(tags) if tags else [])


def gauge(metric: str, value: float, tags: Sequence[str] | None = None) -> None:
    if _client is not None:
        _client.gauge(metric, value, tags=list(tags) if tags else [])
