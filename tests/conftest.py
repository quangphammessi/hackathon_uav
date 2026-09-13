"""Shared test fixtures.

Two tiers, and the split is deliberate:

* **unit** -- memory/json/tfidf adapters only. No database, no broker, no
  model server. Fast, hermetic, and they test the logic we wrote.
* **integration** (`@pytest.mark.integration`) -- real Postgres with pgvector
  and real Redis. These test the things that only break against real
  infrastructure: SQL that is wrong, a hash chain that does not survive a
  concurrent write, a vector query that returns nothing because the index
  filter is inverted. Skipped automatically when the services are absent, so
  the suite still passes on a laptop with nothing installed.

A mocked database would make the integration tier pointless: the bugs it
exists to catch are precisely the ones a mock is defined not to have.
"""
from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

# The unit tier must never accidentally reach real infrastructure, so the
# lightweight adapters are selected before agentmarket_core.config is imported.
os.environ.setdefault("EVENT_BUS_BACKEND", "memory")
os.environ.setdefault("FEATURE_STORE_BACKEND", "memory")
os.environ.setdefault("PRODUCT_STORE_BACKEND", "json")
os.environ.setdefault("VECTOR_STORE_BACKEND", "tfidf")
os.environ.setdefault("EMBEDDINGS_BACKEND", "hash")
os.environ.setdefault("AGENTMARKET_LLM_PROVIDER", "none")

TEST_SKU = "0950600013435"


def _provision_test_database() -> None:
    """Point this test process at a dedicated `*_test` database.

    The integration tier truncates and rewrites domain tables. Pointed at the
    same database the services use, it would destroy a running stack's data
    mid-run -- and the end-to-end tests, which drive that stack over HTTP,
    would then fail for reasons that have nothing to do with the code under
    test. Redirecting DATABASE_URL here, before agentmarket_core.config is
    imported, keeps the two tiers from fighting over one database.
    """
    source = os.getenv("DATABASE_URL", "postgresql://agentmarket:agentmarket@localhost:5432/agentmarket")
    if os.getenv("TEST_DATABASE_URL"):
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
        return

    parts = urlsplit(source)
    name = (parts.path.lstrip("/") or "agentmarket")
    if name.endswith("_test"):
        return
    test_url = urlunsplit(parts._replace(path=f"/{name}_test"))

    try:
        import psycopg

        with psycopg.connect(source, connect_timeout=2, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (f"{name}_test",)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{name}_test"')
        with psycopg.connect(test_url, connect_timeout=2, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception:  # noqa: BLE001 -- no Postgres: integration tests skip anyway
        pass

    os.environ["DATABASE_URL"] = test_url


_provision_test_database()


def _postgres_available() -> bool:
    try:
        import psycopg

        from agentmarket_core.config import settings

        with psycopg.connect(settings.database_url, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


def _redis_available() -> bool:
    try:
        import redis

        from agentmarket_core.config import settings

        redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2).ping()
        return True
    except Exception:  # noqa: BLE001
        return False


POSTGRES_UP = _postgres_available()
REDIS_UP = _redis_available()

requires_postgres = pytest.mark.skipif(not POSTGRES_UP, reason="PostgreSQL not reachable")
requires_redis = pytest.mark.skipif(not REDIS_UP, reason="Redis not reachable")


@pytest.fixture
def memory_bus():
    from agentmarket_core.adapters.bus.memory import MemoryEventBus

    return MemoryEventBus(source="test")


@pytest.fixture
def feature_store():
    """A memory feature store pre-seeded with one SKU's commercial features."""
    from agentmarket_core.adapters.featurestore.memory import MemoryFeatureStore

    store = MemoryFeatureStore()
    store.write(TEST_SKU, internal_cost=62.0, map_price=119.0, list_price=179.0, inventory_units=10)
    return store


@pytest.fixture
def pricing_engine(feature_store, memory_bus):
    """Engine with persistence off and a deterministic bandit, so tests assert
    on guardrail behaviour rather than on which spread exploration picked."""
    from agentmarket_core.domain.pricing import PricingEngine

    class FixedBandit:
        def __init__(self, spread: float = 0.03) -> None:
            self.spread = spread
            self.updates: list[tuple] = []

        def choose(self, sku: str) -> float:
            return self.spread

        def update(self, sku: str, spread: float, reward: float) -> None:
            self.updates.append((sku, spread, reward))

    return PricingEngine(store=feature_store, bus=memory_bus, bandit=FixedBandit(), persist=False)


@pytest.fixture
def db_schema():
    """Integration fixture: a migrated database with the domain tables empty.

    Truncating rather than creating a scratch database keeps the fixture fast
    and keeps every test running against the same schema the services use.
    """
    from agentmarket_core import db

    db.migrate()
    db.execute(
        "TRUNCATE trace_spans, orders, negotiations, trust_ledger, trust_tokens, "
        "provenance_events, bandit_arms, quotes, competitor_observations, "
        "product_relations, product_embeddings, products RESTART IDENTITY CASCADE"
    )
    yield db


@pytest.fixture
def seeded_db(db_schema):
    """A database with the demo catalog and provenance events loaded."""
    import json

    from agentmarket_core.config import settings

    products = json.loads((settings.data_dir / "products.json").read_text())
    events = json.loads((settings.data_dir / "supply_chain_events.json").read_text())

    with db_schema.connection() as conn:
        with conn.cursor() as cur:
            for p in products:
                cur.execute(
                    """INSERT INTO products (sku, gtin, name, category, description, attributes,
                                             currency, claims, role, internal_cost, map_price,
                                             list_price, inventory_units, batch)
                       VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)""",
                    (p["sku"], p["gtin"], p["name"], p["category"], p["description"],
                     json.dumps(p.get("attributes", {})), p.get("currency", "AUD"),
                     json.dumps(p.get("claims", [])), p.get("role", "core"),
                     p["internal_cost"], p["map_price"], p["list_price"],
                     p.get("inventory_units", 0), p.get("batch")),
                )
            for e in events:
                cur.execute(
                    """INSERT INTO provenance_events (sku, batch, event_type, biz_step,
                                                      location, actor, event_time, note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    (e["sku"], e["batch"], e["event_type"], e["biz_step"],
                     e["location"], e["actor"], e["ts"], e.get("note")),
                )
    return db_schema


@pytest.fixture
def redis_prefix():
    """A unique Redis keyspace per test, flushed afterwards, so integration
    tests cannot see each other's competitor windows."""
    import redis

    from agentmarket_core.config import settings

    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    prefix = f"amtest:{uuid.uuid4().hex[:8]}"
    yield prefix
    for key in client.scan_iter(f"*{prefix}*"):
        client.delete(key)
