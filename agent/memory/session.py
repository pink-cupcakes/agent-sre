"""Session management — in-memory stub.

Production implementation will back this with DynamoDB (initiative 05).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Union

logger = logging.getLogger(__name__)


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
        logger.info(
            "session created",
            extra={
                "event": "session.created",
                "session.id": session_id,
                "user.id": user_id,
            },
        )
        return session

    def get_or_create(self, session_id: str, user_id: str) -> Session:
        existing = self.get(session_id)
        if existing:
            logger.info(
                "session resumed",
                extra={
                    "event": "session.resumed",
                    "session.id": session_id,
                    "user.id": user_id,
                    "message_count": len(existing.messages),
                },
            )
            return existing
        return self.create(session_id, user_id)

    def save(self, session: Session) -> None:
        session.updated_at = datetime.now(timezone.utc)
        self._sessions[session.session_id] = session

    def count(self) -> int:
        """Return the number of active in-memory sessions."""
        return len(self._sessions)
