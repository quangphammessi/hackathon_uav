"""The event-bus port (proposal §7.2, "asynchronous backbone").

Three adapters implement this interface and are chosen by config alone:

  kafka   -- production. Durable, partitioned, replayable, multi-consumer.
  redis   -- Redis Streams. A real network broker with consumer groups and
             durable history, but one you already have running for the feature
             store. The sensible choice for a laptop demo or a small
             deployment that does not want a Kafka cluster to babysit.
  memory  -- in-process direct dispatch. For unit tests, where a broker would
             add startup cost and flakiness without testing anything the code
             under test owns.

Every adapter guarantees the same three things, which is what lets callers
stay ignorant of which one is live: `publish` returns the fully-formed
envelope it wrote (so the caller can log/return the event_id), handlers
registered via `subscribe` are invoked with that same envelope, and a handler
raising an exception never kills the consumer loop or blocks the publisher.
"""
from __future__ import annotations

import abc
import logging
import time
import uuid
from typing import Any, Callable

log = logging.getLogger("agentmarket.bus")

EventHandler = Callable[[dict], None]


def make_envelope(topic: str, payload: dict, source: str) -> dict:
    """The wire format. Deliberately flat and self-describing: a consumer can
    route, de-duplicate (event_id) and age out (ts) a message without
    understanding the payload schema."""
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "topic": topic,
        "ts": time.time(),
        "source": source,
        "payload": payload,
    }


class EventBus(abc.ABC):
    """Abstract bus. Subclasses implement _emit and the consumer loop."""

    def __init__(self, source: str) -> None:
        self.source = source
        self._handlers: dict[str, list[EventHandler]] = {}
        self._running = False

    # --- producer side -----------------------------------------------------
    def publish(self, topic: str, payload: dict) -> dict:
        envelope = make_envelope(topic, payload, self.source)
        self._emit(topic, envelope)
        return envelope

    @abc.abstractmethod
    def _emit(self, topic: str, envelope: dict) -> None: ...

    # --- consumer side -----------------------------------------------------
    def subscribe(self, topic: str, handler: EventHandler) -> None:
        """Register a handler. Call before `start()`; adapters that need to
        tell a broker which topics to join read this map at start time."""
        self._handlers.setdefault(topic, []).append(handler)

    @property
    def subscribed_topics(self) -> list[str]:
        return list(self._handlers.keys())

    def _dispatch(self, envelope: dict) -> None:
        """Fan an envelope out to every handler for its topic.

        A handler that raises is logged and skipped: one bad consumer must not
        take down the consumer loop, and must not stop its siblings from
        seeing the same message.
        """
        for handler in self._handlers.get(envelope.get("topic", ""), []):
            try:
                handler(envelope)
            except Exception:  # noqa: BLE001 -- isolate handler failures
                log.exception("event handler failed for topic=%s", envelope.get("topic"))

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    @abc.abstractmethod
    def health(self) -> dict[str, Any]: ...
