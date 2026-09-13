"""In-memory feature store for unit tests.

Mirrors the Redis adapter's semantics exactly -- one current price per
competitor (not an append log), same outlier policy inherited from the base
class -- so a test that passes here is testing the same behaviour that runs
in production, not a simplified cousin of it.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from agentmarket_core.adapters.featurestore import FeatureStore


class MemoryFeatureStore(FeatureStore):
    def __init__(self) -> None:
        self._features: dict[str, dict[str, Any]] = {}
        self._competitors: dict[str, dict[str, float]] = {}
        self._history: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def write(self, sku: str, **features: Any) -> None:
        with self._lock:
            self._features.setdefault(sku, {}).update(features)

    def get_online(self, sku: str) -> dict:
        with self._lock:
            out = dict(self._features.get(sku, {}))
            prices = sorted(self._competitors.get(sku, {}).values())
        out["competitor_prices"] = prices
        out["competitor_count"] = len(prices)
        return out

    def competitor_prices(self, sku: str) -> list[float]:
        with self._lock:
            return sorted(self._competitors.get(sku, {}).values())

    def record_competitor_price(
        self, sku: str, competitor: str, price: float, accepted: bool, reason: str | None
    ) -> None:
        with self._lock:
            self._history.setdefault(sku, []).append({
                "competitor": competitor, "price": price, "accepted": accepted,
                "reason": reason, "observed_at": time.time(),
            })
            if accepted:
                self._competitors.setdefault(sku, {})[competitor] = price

    def competitor_detail(self, sku: str) -> list[dict]:
        with self._lock:
            return [{"competitor": c, "price": p, "observed_at": 0.0}
                    for c, p in self._competitors.get(sku, {}).items()]

    def offline_history(self, sku: str, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(reversed(self._history.get(sku, [])))[:limit]

    def health(self) -> dict:
        return {"backend": "memory", "status": "ok", "skus": len(self._features)}
