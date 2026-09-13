"""LLMOps tracing (proposal §5.5).

Spans are shaped like OpenTelemetry GenAI spans and go two places: Postgres
(durable, queryable -- "show me every run where the repair node fired twice")
and the event bus (live, so the Ops Dashboard can animate a run as it
happens). Same span, two consumers with genuinely different needs; trying to
serve both from one store gives you either a slow dashboard or a lossy audit
trail.

The span context manager never raises out of the traced block. An
observability failure that takes down the thing it observes is worse than no
observability, so a write error is logged and swallowed.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from agentmarket_core import db
from agentmarket_core.adapters.bus import EventBus
from agentmarket_core.config import TOPIC_TRACE_SPAN, settings

log = logging.getLogger("agentmarket.tracing")


def new_trace_id() -> str:
    return f"trace_{uuid.uuid4().hex[:12]}"


class Tracer:
    def __init__(self, bus: EventBus | None = None, service: str | None = None, persist: bool = True) -> None:
        self.bus = bus
        self.service = service or settings.service_name
        self.persist = persist

    @contextmanager
    def span(self, trace_id: str, name: str, attributes: dict | None = None) -> Iterator[dict]:
        """Trace one node. Yields a mutable dict the body fills in with
        outputs; whatever is in it at exit becomes the span's attributes."""
        span_id = f"span_{uuid.uuid4().hex[:10]}"
        started = time.time()
        payload: dict[str, Any] = dict(attributes or {})
        status = "OK"
        try:
            yield payload
        except Exception as exc:
            status = "ERROR"
            payload["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            ended = time.time()
            record = {
                "trace_id": trace_id, "span_id": span_id, "name": name,
                "service": self.service, "started_at": started, "ended_at": ended,
                "duration_ms": round((ended - started) * 1000, 3),
                "status": status, "attributes": payload,
            }
            self._emit(record)

    def _emit(self, record: dict) -> None:
        if self.persist:
            try:
                db.execute(
                    """INSERT INTO trace_spans (span_id, trace_id, name, service, started_at,
                                                ended_at, duration_ms, status, attributes)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                    (record["span_id"], record["trace_id"], record["name"], record["service"],
                     record["started_at"], record["ended_at"], record["duration_ms"],
                     record["status"], json.dumps(record["attributes"], default=str)),
                )
            except Exception:  # noqa: BLE001 -- never break the traced path
                log.debug("span persist failed", exc_info=True)
        if self.bus:
            try:
                self.bus.publish(TOPIC_TRACE_SPAN, record)
            except Exception:  # noqa: BLE001
                log.debug("span publish failed", exc_info=True)

    @staticmethod
    def spans(trace_id: str) -> list[dict]:
        return db.query(
            """SELECT span_id, trace_id, name, service, started_at, ended_at,
                      duration_ms, status, attributes
                 FROM trace_spans WHERE trace_id = %s ORDER BY started_at""",
            (trace_id,),
        )

    @staticmethod
    def recent_traces(limit: int = 25) -> list[dict]:
        return db.query(
            """SELECT trace_id,
                      min(started_at)                              AS started_at,
                      sum(duration_ms)                             AS total_ms,
                      count(*)                                     AS spans,
                      bool_or(status = 'ERROR')                    AS had_error,
                      max(attributes ->> 'outcome')                AS outcome,
                      max(attributes ->> 'query')                  AS query
                 FROM trace_spans
                GROUP BY trace_id
                ORDER BY min(started_at) DESC
                LIMIT %s""",
            (limit,),
        )
