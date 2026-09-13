"""Event-bus factory. `build_bus()` is the only thing services import."""
from __future__ import annotations

import logging

from agentmarket_core.adapters.bus.base import EventBus, EventHandler, make_envelope
from agentmarket_core.adapters.bus.memory import MemoryEventBus
from agentmarket_core.config import settings

log = logging.getLogger("agentmarket.bus")

__all__ = ["EventBus", "EventHandler", "make_envelope", "MemoryEventBus", "build_bus"]


def build_bus(source: str, backend: str | None = None, consumer_group: str | None = None) -> EventBus:
    """Construct the configured bus.

    `source` names the publishing service (it lands in every envelope, which
    is what makes an event trail readable after the fact). `consumer_group`
    defaults to the service name so each service independently sees every
    message, and scaling one service to N replicas splits its load rather
    than duplicating it.
    """
    backend = (backend or settings.event_bus_backend).lower()
    group = consumer_group or f"{settings.kafka_consumer_group}-{source}"

    if backend == "memory":
        return MemoryEventBus(source=source)

    if backend == "redis":
        from agentmarket_core.adapters.bus.redis_streams import RedisStreamsEventBus

        return RedisStreamsEventBus(url=settings.redis_url, source=source, consumer_group=group)

    if backend == "kafka":
        from agentmarket_core.adapters.bus.kafka_bus import KafkaEventBus

        return KafkaEventBus(
            bootstrap_servers=settings.kafka_bootstrap_servers, source=source, consumer_group=group
        )

    raise ValueError(
        f"unknown EVENT_BUS_BACKEND={backend!r}; expected one of: kafka, redis, memory"
    )
