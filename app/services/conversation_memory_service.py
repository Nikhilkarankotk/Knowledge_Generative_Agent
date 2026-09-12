"""Equivalent of ``ConversationMemoryService.java``.

The Java class keeps a ``ConcurrentHashMap`` of session histories in memory
(max. 10 exchanges). This module is the Python equivalent using a plain dict guarded
by a lock. Memory is intentionally **not** persisted to the database.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from app.core.config import Settings


class ConversationMemoryService:
    def __init__(self, max_history: int = 10) -> None:
        self._max_history = max_history
        self._lock = threading.Lock()
        self._local_cache: dict[str, deque[dict[str, str]]] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> ConversationMemoryService:
        return cls(max_history=settings.conversation_max_history)

    def add_exchange(self, session_id: str, user_message: str, ai_response: str) -> None:
        exchange = {
            "user": user_message,
            "assistant": ai_response,
            "timestamp": str(int(time.time() * 1000)),
        }
        with self._lock:
            history = self._local_cache.setdefault(session_id, deque())
            history.append(exchange)
            while len(history) > self._max_history:
                history.popleft()

    def get_context(self, session_id: str) -> list[dict[str, str]]:
        with self._lock:
            return list(self._local_cache.get(session_id, ()))
