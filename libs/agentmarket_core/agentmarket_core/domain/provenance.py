"""GS1 EPCIS-style provenance reconciliation (proposal §6.1).

A trust token may only be issued for a (sku, batch) whose event chain is
*complete* -- every required business step present, in order. This module is
the deterministic check that decides that. Nothing here is probabilistic and
nothing here calls a model: a missing quality-control event is a missing
quality-control event, and the answer is no.

`event_chain_hash` is what binds the issued credential to the exact set of
events that justified it. If someone later edits or back-dates a provenance
event, the recomputed hash no longer matches the one inside the signed
credential, and verification fails. That is the tamper-evidence property,
and it costs one SHA-256 rather than a distributed ledger.
"""
from __future__ import annotations

import hashlib
import json

from agentmarket_core import db
from agentmarket_core.config import settings


def default_batch_for(sku: str) -> str | None:
    """The batch a SKU currently ships from. In a real deployment this comes
    from the WMS pick; here it is the newest batch we hold events for."""
    row = db.query_one(
        """SELECT batch FROM provenance_events WHERE sku = %s
            GROUP BY batch ORDER BY max(event_time) DESC LIMIT 1""",
        (sku,),
    )
    if row:
        return row["batch"]
    row = db.query_one("SELECT batch FROM products WHERE sku = %s", (sku,))
    return row["batch"] if row else None


def events_for(sku: str, batch: str) -> list[dict]:
    return db.query(
        """SELECT sku, batch, event_type, biz_step, location, actor,
                  to_char(event_time, 'YYYY-MM-DD"T"HH24:MI:SSOF') AS ts, note
             FROM provenance_events
            WHERE sku = %s AND batch = %s
            ORDER BY event_time""",
        (sku, batch),
    )


def event_chain_hash(events: list[dict]) -> str:
    """Order-independent, formatting-independent digest of an event chain.

    Sorting the per-event digests before combining them means two systems
    that hold the same events in a different order still agree on the hash,
    while any change to any field changes it. `sort_keys` does the same job
    for key ordering within an event.
    """
    digests = sorted(
        hashlib.sha256(
            json.dumps(
                {k: e[k] for k in ("sku", "batch", "event_type", "biz_step", "location", "actor", "ts")},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        for e in events
    )
    return hashlib.sha256("".join(digests).encode()).hexdigest()


def chain_status(sku: str, batch: str | None = None) -> dict:
    """Is this (sku, batch) eligible for a trust token, and if not, why not?"""
    batch = batch or default_batch_for(sku)
    if not batch:
        return {
            "sku": sku, "batch": None, "ready": False,
            "missing_steps": settings.required_biz_steps, "present_steps": [],
            "event_chain_hash": None, "events": [],
            "reason_code": "NO_PROVENANCE_RECORD",
        }

    events = events_for(sku, batch)
    present = [e["biz_step"] for e in events]
    missing = [step for step in settings.required_biz_steps if step not in present]
    ready = not missing and bool(events)
    return {
        "sku": sku,
        "batch": batch,
        "ready": ready,
        "missing_steps": missing,
        "present_steps": present,
        "event_chain_hash": event_chain_hash(events) if events else None,
        "events": events,
        "reason_code": None if ready else ("CHAIN_GAP" if events else "NO_PROVENANCE_RECORD"),
    }


def all_chain_statuses() -> list[dict]:
    """Readiness across the catalog -- the Ops Dashboard's provenance table."""
    rows = db.query("SELECT sku, name FROM products ORDER BY sku")
    out = []
    for r in rows:
        status = chain_status(r["sku"])
        status["name"] = r["name"]
        status.pop("events", None)  # summary view; the detail endpoint returns events
        out.append(status)
    return out
