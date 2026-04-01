"""Session management — in-memory stub.

Production implementation will back this with DynamoDB (initiative 05).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Union


@dataclass
class Session:
    session_id: str
    user_id: str
    messages: list[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def add_message(self, role: str, content: Union[str, list]) -> None:
        self.messages.append({"role": role, "content": content})
        self.updated_at = datetime.now(timezone.utc)


class SessionStore:
    """In-memory session store. Replace with DynamoDB in production."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def create(self, session_id: str, user_id: str) -> Session:
        session = Session(session_id=session_id, user_id=user_id)
        self._sessions[session_id] = session
        return session

    def get_or_create(self, session_id: str, user_id: str) -> Session:
        return self.get(session_id) or self.create(session_id, user_id)

    def save(self, session: Session) -> None:
        session.updated_at = datetime.now(timezone.utc)
        self._sessions[session.session_id] = session
