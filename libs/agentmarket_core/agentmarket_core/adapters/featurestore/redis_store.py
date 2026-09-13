"""Redis-backed online feature store, Postgres-backed offline history.

Key layout (flat and greppable, which matters when you are debugging a
pricing incident with redis-cli at 2am):

    am:features:{sku}          HASH        scalar features (cost, MAP, list, inventory)
    am:competitors:{sku}       ZSET        member="{competitor}", score=price
    am:competitors:{sku}:ts    HASH        competitor -> last observation time

A sorted set rather than a list for competitor prices: we only ever want the
*current* price per competitor, not a log of every scrape, and a ZSET gives
us "one entry per competitor, latest value wins" plus ordered reads for free.
The append-only log lives in Postgres, which is the right store for it.
"""
from __future__ import annotations

import json
import time
from typing import Any

import redis

from agentmarket_core.adapters.featurestore import FeatureStore

FEATURE_TTL_SECONDS = 7 * 24 * 3600  # online store is a cache; the warehouse is the record


class RedisFeatureStore(FeatureStore):
    def __init__(self, url: str) -> None:
        self._r = redis.Redis.from_url(url, decode_responses=True)

    @staticmethod
    def _fkey(sku: str) -> str:
        return f"am:features:{sku}"

    @staticmethod
    def _ckey(sku: str) -> str:
        return f"am:competitors:{sku}"

    def write(self, sku: str, **features: Any) -> None:
        if not features:
            return
        # JSON-encode values so floats/ints/bools survive the round trip with
        # their types intact; Redis hashes are stringly-typed otherwise.
        mapping = {k: json.dumps(v) for k, v in features.items()}
        pipe = self._r.pipeline()
        pipe.hset(self._fkey(sku), mapping=mapping)
        pipe.expire(self._fkey(sku), FEATURE_TTL_SECONDS)
        pipe.execute()

    def get_online(self, sku: str) -> dict:
        raw = self._r.hgetall(self._fkey(sku))
        out: dict[str, Any] = {}
        for k, v in raw.items():
            try:
                out[k] = json.loads(v)
            except json.JSONDecodeError:
                out[k] = v
        prices = self.competitor_prices(sku)
        out["competitor_prices"] = prices
        out["competitor_count"] = len(prices)
        return out

    def competitor_prices(self, sku: str) -> list[float]:
        return [float(score) for _member, score in self._r.zrange(self._ckey(sku), 0, -1, withscores=True)]

    def record_competitor_price(
        self, sku: str, competitor: str, price: float, accepted: bool, reason: str | None
    ) -> None:
        # Offline first: the warehouse records every observation, including
        # rejects, because the rejects are the audit trail for the filter.
        try:
            from agentmarket_core import db

            db.execute(
                """INSERT INTO competitor_observations (sku, competitor, price, accepted, reason)
                   VALUES (%s, %s, %s, %s, %s)""",
                (sku, competitor, price, accepted, reason),
            )
        except Exception:  # noqa: BLE001 -- offline history must never break the hot path
            pass

        if not accepted:
            return
        pipe = self._r.pipeline()
        pipe.zadd(self._ckey(sku), {competitor: price})
        pipe.hset(f"{self._ckey(sku)}:ts", competitor, time.time())
        pipe.expire(self._ckey(sku), FEATURE_TTL_SECONDS)
        pipe.expire(f"{self._ckey(sku)}:ts", FEATURE_TTL_SECONDS)
        pipe.execute()

    def competitor_detail(self, sku: str) -> list[dict]:
        """Per-competitor current prices -- what the Ops Dashboard charts."""
        rows = self._r.zrange(self._ckey(sku), 0, -1, withscores=True)
        seen_at = self._r.hgetall(f"{self._ckey(sku)}:ts")
        return [
            {"competitor": member, "price": float(score), "observed_at": float(seen_at.get(member, 0) or 0)}
            for member, score in rows
        ]

    def offline_history(self, sku: str, limit: int = 100) -> list[dict]:
        from agentmarket_core import db

        return db.query(
            """SELECT competitor, price::float AS price, accepted, reason, observed_at
                 FROM competitor_observations
                WHERE sku = %s
                ORDER BY observed_at DESC
                LIMIT %s""",
            (sku, limit),
        )

    def health(self) -> dict:
        try:
            self._r.ping()
            return {"backend": "redis", "status": "ok"}
        except redis.RedisError as exc:
            return {"backend": "redis", "status": "degraded", "detail": str(exc)}
