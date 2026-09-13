#!/usr/bin/env python3
"""Create the Kafka topics with explicit partitioning and retention.

Auto-creation exists, but it gives every topic the broker defaults: one
partition and generic retention. Partition count is the ceiling on a topic's
consumer parallelism and it cannot be lowered later, so it is worth setting
deliberately -- the busy topics (competitor signals, trace spans) get room to
scale out, the low-volume ones stay at one partition and keep total ordering.

Idempotent: topics that already exist are left alone.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentmarket_core.config import (  # noqa: E402
    TOPIC_COMPETITOR_SIGNAL,
    TOPIC_PAYMENT_REJECTED,
    TOPIC_PAYMENT_SETTLED,
    TOPIC_QUOTE_ISSUED,
    TOPIC_STOREFRONT_OFFER,
    TOPIC_STOREFRONT_REJECTION,
    TOPIC_TRACE_SPAN,
    TOPIC_TRANSACTION_OUTCOME,
    TOPIC_TRUST_ISSUED,
    TOPIC_TRUST_REVOKED,
    settings,
)

WEEK_MS = str(7 * 24 * 60 * 60 * 1000)
DAY_MS = str(24 * 60 * 60 * 1000)

# (topic, partitions, retention_ms)
TOPIC_SPEC = [
    (TOPIC_COMPETITOR_SIGNAL, 6, WEEK_MS),     # highest volume; keyed by SKU
    (TOPIC_QUOTE_ISSUED, 6, DAY_MS),
    (TOPIC_TRANSACTION_OUTCOME, 3, WEEK_MS),   # feeds the bandit; keep longer
    (TOPIC_TRUST_ISSUED, 3, WEEK_MS),
    (TOPIC_TRUST_REVOKED, 1, WEEK_MS),         # rare, and total order is useful
    (TOPIC_STOREFRONT_OFFER, 3, DAY_MS),
    (TOPIC_STOREFRONT_REJECTION, 3, DAY_MS),
    (TOPIC_PAYMENT_SETTLED, 3, WEEK_MS),
    (TOPIC_PAYMENT_REJECTED, 3, WEEK_MS),
    (TOPIC_TRACE_SPAN, 6, DAY_MS),             # highest volume, shortest life
]


def main() -> None:
    if settings.event_bus_backend != "kafka":
        print(f"EVENT_BUS_BACKEND={settings.event_bus_backend} -- no Kafka topics to create")
        return

    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": settings.kafka_bootstrap_servers})

    # The broker may still be forming its quorum when this runs.
    for attempt in range(30):
        try:
            existing = set(admin.list_topics(timeout=5).topics)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 29:
                raise RuntimeError(f"Kafka not reachable: {exc}") from exc
            time.sleep(2)

    wanted = [
        NewTopic(name, num_partitions=parts, replication_factor=1,
                 config={"retention.ms": retention, "cleanup.policy": "delete"})
        for name, parts, retention in TOPIC_SPEC
        if name not in existing
    ]
    if not wanted:
        print(f"all {len(TOPIC_SPEC)} topics already exist")
        return

    for name, future in admin.create_topics(wanted).items():
        try:
            future.result()
            print(f"created topic {name}")
        except Exception as exc:  # noqa: BLE001
            if "already exists" in str(exc).lower():
                print(f"topic {name} already exists")
            else:
                raise


if __name__ == "__main__":
    main()
