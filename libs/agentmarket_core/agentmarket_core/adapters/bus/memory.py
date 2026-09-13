"""In-process bus. Publish dispatches synchronously to local handlers.

Used by the unit tests and by `EVENT_BUS_BACKEND=memory`. Synchronous
dispatch is a deliberate choice here rather than a shortcut: it makes tests
deterministic (no "sleep and hope the consumer caught up"), and the
production adapters' asynchrony is exercised by the integration tests
instead.
"""
from __future__ import annotations

import threading
from typing import Any

from agentmarket_core.adapters.bus.base import EventBus


class MemoryEventBus(EventBus):
    def __init__(self, source: str = "memory", history_limit: int = 500) -> None:
        super().__init__(source)
        self._history: list[dict] = []
        self._history_limit = history_limit
        self._lock = threading.Lock()

    def _emit(self, topic: str, envelope: dict) -> None:
        with self._lock:
            self._history.append(envelope)
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]
        self._dispatch(envelope)

    def recent(self, topic: str | None = None, limit: int = 50) -> list[dict]:
        with self._lock:
            items = [e for e in self._history if topic is None or e["topic"] == topic]
        return items[-limit:][::-1]

    def health(self) -> dict[str, Any]:
        return {"backend": "memory", "status": "ok", "buffered_events": len(self._history)}
