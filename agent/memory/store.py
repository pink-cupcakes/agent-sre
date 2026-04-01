"""Agent memory — in-memory stub.

Production implementation will integrate with a vector store or DynamoDB.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MemoryEntry:
    key: str
    value: str
    metadata: dict = field(default_factory=dict)


class MemoryStore:
    """Key-value memory store for cross-session context. In-memory stub."""

    def __init__(self) -> None:
        self._store: dict[str, MemoryEntry] = {}

    def set(self, key: str, value: str, metadata: dict | None = None) -> None:
        self._store[key] = MemoryEntry(key=key, value=value, metadata=metadata or {})

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        return entry.value if entry else None

    def delete(self, key: str) -> None:
        self._store.pop(key, None)
