"""Redis Streams event bus.

Redis Streams is an append-only log with consumer groups, blocking reads and
retained history -- the same shape as Kafka, one process instead of a
cluster. This adapter exists so the full distributed code path (network
broker, background consumer thread, at-least-once delivery, offset commits)
is exercised in environments where standing up Kafka is not worth it, rather
than silently degrading to in-process calls and hiding concurrency bugs until
production.

Delivery semantics match the Kafka adapter deliberately: a consumer group per
service (so each service sees every message once, and scaling a service to N
replicas splits its partitions), XACK only after the handler returns, and a
handler exception leaves the message un-acked for redelivery.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

import redis

from agentmarket_core.adapters.bus.base import EventBus

log = logging.getLogger("agentmarket.bus.redis")

# Cap each stream so a long-running demo cannot grow Redis without bound.
# MAXLEN ~ is approximate trimming: far cheaper than exact, and the bound is
# advisory anyway.
STREAM_MAXLEN = 10_000


class RedisStreamsEventBus(EventBus):
    def __init__(self, url: str, source: str, consumer_group: str) -> None:
        super().__init__(source)
        self._client = redis.Redis.from_url(url, decode_responses=True)
        self._group = consumer_group
        self._consumer_name = f"{source}-{threading.get_ident()}"
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def _emit(self, topic: str, envelope: dict) -> None:
        self._client.xadd(
            name=topic,
            fields={"data": json.dumps(envelope)},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )

    def _ensure_groups(self) -> None:
        for topic in self.subscribed_topics:
            try:
                # mkstream=True creates the stream if this service starts
                # before anything has ever published to the topic.
                self._client.xgroup_create(name=topic, groupname=self._group, id="$", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    def start(self) -> None:
        if not self.subscribed_topics:
            super().start()
            return
        self._ensure_groups()
        super().start()
        self._thread = threading.Thread(target=self._consume_loop, name="redis-bus-consumer", daemon=True)
        self._thread.start()
        log.info("redis-streams consumer started group=%s topics=%s", self._group, self.subscribed_topics)

    def _consume_loop(self) -> None:
        streams = {topic: ">" for topic in self.subscribed_topics}
        while not self._stop_event.is_set():
            try:
                response = self._client.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer_name,
                    streams=streams,
                    count=50,
                    block=1000,  # ms; bounded so stop() is responsive
                )
            except redis.RedisError:
                log.exception("redis xreadgroup failed; retrying")
                self._stop_event.wait(1.0)
                continue

            for topic, messages in response or []:
                for message_id, fields in messages:
                    try:
                        envelope = json.loads(fields["data"])
                    except (KeyError, json.JSONDecodeError):
                        log.warning("undecodable message on %s id=%s; acking to skip", topic, message_id)
                        self._client.xack(topic, self._group, message_id)
                        continue
                    try:
                        self._dispatch(envelope)
                    finally:
                        # _dispatch swallows handler errors, so reaching here
                        # means "delivered as well as we are going to".
                        self._client.xack(topic, self._group, message_id)

    def stop(self) -> None:
        self._stop_event.set()
        super().stop()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def recent(self, topic: str | None = None, limit: int = 50) -> list[dict]:
        """Read back recent history straight off the stream(s)."""
        topics = [topic] if topic else self.subscribed_topics
        out: list[dict] = []
        for name in topics:
            try:
                for _id, fields in self._client.xrevrange(name, count=limit):
                    out.append(json.loads(fields["data"]))
            except (redis.RedisError, json.JSONDecodeError, KeyError):
                continue
        out.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return out[:limit]

    def health(self) -> dict[str, Any]:
        try:
            self._client.ping()
            return {"backend": "redis-streams", "status": "ok", "group": self._group}
        except redis.RedisError as exc:
            return {"backend": "redis-streams", "status": "degraded", "detail": str(exc)}
