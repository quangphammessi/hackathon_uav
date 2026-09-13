"""Hash-anchored, append-only trust-token ledger (proposal §6.2).

Each row commits to the row before it: `entry_hash = SHA256(prev_hash ||
canonical(payload))`. Altering or deleting any historical row breaks every
hash after it, and `verify_chain_integrity()` recomputes the whole chain to
prove it. Appending is O(1); verifying is O(n) and is meant to be run as an
audit, not per request.

This is the part of the design that answers "why not a blockchain?". What a
blockchain buys is tamper-evidence *without a trusted operator* -- consensus
among parties who do not trust each other. Here the merchant is already the
trusted issuer: the buyer's agent trusts the platform's signature or it does
not transact at all. So the distributed-consensus half is cost with no
benefit, while the hash-chain half -- the part that actually makes tampering
detectable -- is kept. A single SHA-256 per write, no gas, no finality wait,
no public exposure of commercially sensitive supply-chain data.

Appends are serialised with a transaction-scoped advisory lock, and that
choice is load-bearing. Two concurrent issuances that both read the same
`prev_hash` fork the chain, and the corruption is permanent and silent until
someone runs an integrity check. The obvious guard -- `SELECT ... FOR UPDATE`
on the tail row -- is not sufficient here for two independent reasons: the
connection pool runs in autocommit mode, so each statement is its own
transaction and the row lock is released before the INSERT runs; and a row
lock cannot lock a row that does not exist, so it provides no mutual
exclusion at all when the ledger is empty and every writer sees GENESIS.

`pg_advisory_xact_lock` has neither problem. It is held until the surrounding
transaction commits, and it locks a *name* rather than a row, so it works on
an empty table. The explicit `conn.transaction()` block is what gives it a
transaction to be scoped to, since the pool would otherwise autocommit.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from agentmarket_core import db

GENESIS_HASH = "0" * 64

# Arbitrary but fixed 64-bit key identifying the ledger-append lock. Every
# writer in every process must use the same value or the lock does nothing.
LEDGER_ADVISORY_LOCK_KEY = 0x41474D_4C4447


def compute_entry_hash(prev_hash: str, event_type: str, token_id: str, payload: dict) -> str:
    body = json.dumps(
        {"event_type": event_type, "token_id": token_id, "payload": payload},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


class TrustLedger:
    def append(self, event_type: str, token_id: str, payload: dict) -> dict:
        """Append one entry, linked to the current chain tail.

        Read-tail-then-insert must be atomic with respect to other appenders,
        so the whole thing runs inside one transaction holding the advisory
        lock (see module docstring).
        """
        with db.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (LEDGER_ADVISORY_LOCK_KEY,))
                    cur.execute("SELECT entry_hash FROM trust_ledger ORDER BY seq DESC LIMIT 1")
                    tail = cur.fetchone()
                    prev_hash = tail["entry_hash"] if tail else GENESIS_HASH
                    entry_hash = compute_entry_hash(prev_hash, event_type, token_id, payload)
                    cur.execute(
                        """INSERT INTO trust_ledger
                             (event_type, token_id, payload, prev_hash, entry_hash)
                           VALUES (%s,%s,%s::jsonb,%s,%s)
                           RETURNING seq, event_type, token_id, payload, prev_hash, entry_hash,
                                     extract(epoch FROM created_at) AS created_at""",
                        (event_type, token_id, json.dumps(payload), prev_hash, entry_hash),
                    )
                    return dict(cur.fetchone())

    def latest_for(self, token_id: str) -> dict | None:
        return db.query_one(
            "SELECT * FROM trust_ledger WHERE token_id = %s ORDER BY seq DESC LIMIT 1", (token_id,)
        )

    def issue_entry_for(self, token_id: str) -> dict | None:
        return db.query_one(
            "SELECT * FROM trust_ledger WHERE token_id = %s AND event_type = 'ISSUE' "
            "ORDER BY seq LIMIT 1",
            (token_id,),
        )

    def entries(self, limit: int = 100) -> list[dict]:
        return db.query(
            """SELECT seq, event_type, token_id, prev_hash, entry_hash,
                      extract(epoch FROM created_at) AS created_at
                 FROM trust_ledger ORDER BY seq DESC LIMIT %s""",
            (limit,),
        )

    def verify_chain_integrity(self) -> tuple[bool, str | None]:
        """Recompute every entry hash and every link. Returns (ok, error)."""
        rows = db.query(
            "SELECT seq, event_type, token_id, payload, prev_hash, entry_hash "
            "FROM trust_ledger ORDER BY seq"
        )
        expected_prev = GENESIS_HASH
        for row in rows:
            if row["prev_hash"] != expected_prev:
                return False, (
                    f"broken link at seq={row['seq']}: prev_hash={row['prev_hash'][:12]}... "
                    f"expected {expected_prev[:12]}..."
                )
            recomputed = compute_entry_hash(
                row["prev_hash"], row["event_type"], row["token_id"], row["payload"]
            )
            if recomputed != row["entry_hash"]:
                return False, f"tampered payload at seq={row['seq']}: entry_hash mismatch"
            expected_prev = row["entry_hash"]
        return True, None

    def stats(self) -> dict[str, Any]:
        row = db.query_one(
            "SELECT count(*) AS entries, count(*) FILTER (WHERE event_type='ISSUE') AS issued, "
            "count(*) FILTER (WHERE event_type='REVOKE') AS revoked FROM trust_ledger"
        )
        return dict(row or {"entries": 0, "issued": 0, "revoked": 0})


trust_ledger = TrustLedger()
