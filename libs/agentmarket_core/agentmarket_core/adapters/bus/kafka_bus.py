"""Kafka event bus -- the production backbone from proposal §7.2.

Uses confluent-kafka (librdkafka), which is the client that actually holds up
under load; the pure-Python clients are fine until they are not.

Three choices here are worth stating, because they are the ones that decide
whether this behaves correctly in production rather than just in a demo:

* **Keyed messages.** Every event is keyed by its business entity (SKU,
  token_id, trace_id). Kafka guarantees ordering *within a partition*, so
  keying by SKU is what makes "competitor price observed" and "quote issued"
  for the same SKU arrive in the order they happened, while still letting
  different SKUs process in parallel across partitions. Keying randomly --
  or not at all -- silently destroys that guarantee.
* **Manual commit after dispatch.** `enable.auto.commit=false` plus an
  explicit commit once handlers have run gives at-least-once delivery. Auto-
  commit would give at-most-once: an offset committed before a crash means
  the message is simply lost, which for a payment or a revocation is not an
  acceptable failure mode.
* **Producer flush on stop.** librdkafka batches in the background, so a
  process that exits without flushing drops whatever is still in the queue.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from agentmarket_core.adapters.bus.base import EventBus

log = logging.getLogger("agentmarket.bus.kafka")

# Which payload field identifies the entity a topic is ordered by. Anything
# not listed falls back to no key (round-robin), which is correct only for
# events with no per-entity ordering requirement.
TOPIC_KEY_FIELD = {
    "market.competitor_signal": "sku",
    "pricing.quote_issued": "sku",
    "pricing.transaction_outcome": "sku",
    "trust.issued": "sku",
    "trust.revoked": "token_id",
    "storefront.offer": "sku",
    "storefront.rejection": "trace_id",
    "payments.settled": "sku",
    "payments.rejected": "sku",
    "observability.trace_span": "trace_id",
}


class KafkaEventBus(EventBus):
    def __init__(self, bootstrap_servers: str, source: str, consumer_group: str) -> None:
        super().__init__(source)
        from confluent_kafka import Producer  # imported lazily: optional dependency

        self._bootstrap = bootstrap_servers
        self._group = consumer_group
        self._producer = Producer({
            "bootstrap.servers": bootstrap_servers,
            "client.id": source,
            "enable.idempotence": True,   # no duplicates on internal retry
            "acks": "all",                # durable before we call it published
            "linger.ms": 5,               # tiny batching window; large win, invisible latency
            "compression.type": "snappy",
        })
        self._consumer = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @staticmethod
    def _key_for(topic: str, payload: dict) -> str | None:
        field = TOPIC_KEY_FIELD.get(topic)
        if not field:
            return None
        value = payload.get(field)
        return str(value) if value is not None else None

    def _emit(self, topic: str, envelope: dict) -> None:
        key = self._key_for(topic, envelope.get("payload", {}))
        self._producer.produce(
            topic=topic,
            key=key.encode() if key else None,
            value=json.dumps(envelope).encode(),
        )
        # Serve delivery callbacks without blocking the caller.
        self._producer.poll(0)

    def start(self) -> None:
        super().start()
        if not self.subscribed_topics:
            return
        from confluent_kafka import Consumer

        self._consumer = Consumer({
            "bootstrap.servers": self._bootstrap,
            "group.id": self._group,
            "client.id": f"{self.source}-consumer",
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,   # see module docstring
        })
        self._consumer.subscribe(self.subscribed_topics)
        self._thread = threading.Thread(target=self._consume_loop, name="kafka-bus-consumer", daemon=True)
        self._thread.start()
        log.info("kafka consumer started group=%s topics=%s", self._group, self.subscribed_topics)

    def _consume_loop(self) -> None:
        from confluent_kafka import KafkaError

        while not self._stop_event.is_set():
            message = self._consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                # Partition EOF is informational, not a failure.
                if message.error().code() != KafkaError._PARTITION_EOF:  # noqa: SLF001 -- librdkafka's own constant
                    log.error("kafka consume error: %s", message.error())
                continue
            try:
                envelope = json.loads(message.value().decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                log.warning("undecodable kafka message on %s; committing to skip", message.topic())
                self._consumer.commit(message, asynchronous=False)
                continue
            self._dispatch(envelope)
            self._consumer.commit(message, asynchronous=False)

    def stop(self) -> None:
        self._stop_event.set()
        super().stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._consumer is not None:
            try:
                self._consumer.close()
            except Exception:  # noqa: BLE001
                pass
        # Drain anything librdkafka still has queued before the process exits.
        self._producer.flush(5.0)

    def recent(self, topic: str | None = None, limit: int = 50) -> list[dict]:
        """Kafka is not a query store -- reading "the last N" means seeking a
        consumer backwards across partitions, which is the wrong tool. The
        gateway keeps a live ring buffer fed by its subscription instead, and
        `trace_spans` / the domain tables in Postgres hold the durable
        history the UI actually queries."""
        return []

    def health(self) -> dict[str, Any]:
        try:
            metadata = self._producer.list_topics(timeout=3.0)
            return {
                "backend": "kafka",
                "status": "ok",
                "brokers": len(metadata.brokers),
                "group": self._group,
            }
        except Exception as exc:  # noqa: BLE001
            return {"backend": "kafka", "status": "degraded", "detail": str(exc)}
