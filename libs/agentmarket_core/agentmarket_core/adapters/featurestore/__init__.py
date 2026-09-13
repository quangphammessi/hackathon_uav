"""Feature store port + factory (proposal §4.1).

The online/offline split is the whole point of a feature store and is
preserved exactly here:

* **Online** (Redis): the features the pricing engine reads on the hot path,
  at single-digit-millisecond latency, for one SKU at a time. Redis hashes
  for scalar features, one sorted set per SKU for the rolling window of
  competitor prices.
* **Offline** (Postgres): the full retained history -- every observation,
  including the ones the outlier filter rejected -- used for backtesting the
  pricing policy and auditing why a price moved. Never read on the hot path.

Writes go to both. That dual-write is what Feast does for you in production
(`materialize`); doing it explicitly here keeps the interface honest about
the fact that two stores exist and can disagree.
"""
from __future__ import annotations

import abc
import statistics
import time
from typing import Any

from agentmarket_core.config import settings

# How many prior accepted observations a SKU needs before the outlier filter
# is allowed to reject anything. Below this the median is not a meaningful
# reference point, so observations are admitted to bootstrap the window.
MIN_OUTLIER_WINDOW = 2


class FeatureStore(abc.ABC):
    """Port. `ingest_competitor_price` owns the outlier decision because the
    filter needs the trailing window, and the window lives here."""

    @abc.abstractmethod
    def write(self, sku: str, **features: Any) -> None: ...

    @abc.abstractmethod
    def get_online(self, sku: str) -> dict: ...

    @abc.abstractmethod
    def competitor_prices(self, sku: str) -> list[float]: ...

    @abc.abstractmethod
    def record_competitor_price(self, sku: str, competitor: str, price: float, accepted: bool, reason: str | None) -> None: ...

    @abc.abstractmethod
    def offline_history(self, sku: str, limit: int = 100) -> list[dict]: ...

    @abc.abstractmethod
    def health(self) -> dict: ...

    # --- shared policy, identical across adapters -------------------------
    def evaluate_outlier(self, sku: str, price: float) -> tuple[bool, str | None]:
        """Decide whether an observation is allowed to move our fair value.

        A scraper that breaks and reports $6.00 for a $140 boot must not drag
        the whole market model down with it (proposal §4.1). The filter is
        deliberately a deviation-from-trailing-median test rather than a
        standard-deviation test: the median is robust to exactly the kind of
        contamination we are filtering, whereas a mean/σ test is dragged by
        the outlier it is supposed to catch.

        A SKU's first observations are accepted unconditionally: there is no
        trailing window to judge them against yet, and refusing to bootstrap
        would mean never pricing a new SKU at all. Two prior observations is
        the minimum that gives the median any meaning at all, and waiting for
        more would leave a fresh SKU unprotected for longer than necessary.
        """
        window = self.competitor_prices(sku)
        if len(window) < MIN_OUTLIER_WINDOW:
            return True, None
        median = statistics.median(window)
        if median <= 0:
            return True, None
        deviation = abs(price - median) / median
        if deviation > settings.competitor_outlier_deviation:
            return False, (
                f"outlier: {deviation:.0%} deviation from trailing median {median:.2f}"
            )
        return True, None

    def ingest_competitor_price(self, sku: str, competitor: str, price: float) -> tuple[bool, str | None]:
        """Full ingest path: judge, then persist to offline regardless, then
        admit to the online window only if accepted."""
        accepted, reason = self.evaluate_outlier(sku, price)
        self.record_competitor_price(sku, competitor, price, accepted, reason)
        return accepted, reason


def build_feature_store(backend: str | None = None) -> FeatureStore:
    backend = (backend or settings.feature_store_backend).lower()
    if backend == "redis":
        from agentmarket_core.adapters.featurestore.redis_store import RedisFeatureStore

        return RedisFeatureStore(url=settings.redis_url)
    if backend == "memory":
        from agentmarket_core.adapters.featurestore.memory import MemoryFeatureStore

        return MemoryFeatureStore()
    raise ValueError(f"unknown FEATURE_STORE_BACKEND={backend!r}; expected one of: redis, memory")


__all__ = ["FeatureStore", "build_feature_store", "time"]
