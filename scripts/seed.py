#!/usr/bin/env python3
"""Apply the schema and load the demo catalog, competitor feed and provenance
events into Postgres, then build the vector index.

Idempotent: every write is an upsert, so re-running it after changing
data/*.json refreshes the stack without a teardown. This is the step Compose
runs once at startup (the `seed` one-shot service) and the step you re-run by
hand after editing the mock data.

    python scripts/seed.py              # schema + data + embeddings
    python scripts/seed.py --reembed    # only rebuild the vector index
    python scripts/seed.py --reset      # truncate domain tables first
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentmarket_core import db
from agentmarket_core.adapters.embeddings import build_embedder
from agentmarket_core.adapters.productstore import document_text
from agentmarket_core.adapters.vectorstore.pgvector_store import PgVectorStore
from agentmarket_core.config import settings
from agentmarket_core.logging_setup import configure_logging

TABLES_TO_RESET = [
    "trace_spans", "orders", "negotiations", "trust_ledger", "trust_tokens",
    "provenance_events", "bandit_arms", "quotes", "competitor_observations",
    "product_relations", "product_embeddings", "products",
]


def load_json(name: str) -> list[dict]:
    return json.loads((settings.data_dir / name).read_text())


def seed_products() -> int:
    rows = load_json("products.json")
    with db.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """INSERT INTO products
                         (sku, gtin, name, category, description, attributes, currency,
                          claims, role, internal_cost, map_price, list_price,
                          inventory_units, batch, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s,%s,%s, now())
                       ON CONFLICT (sku) DO UPDATE SET
                         gtin=EXCLUDED.gtin, name=EXCLUDED.name, category=EXCLUDED.category,
                         description=EXCLUDED.description, attributes=EXCLUDED.attributes,
                         currency=EXCLUDED.currency, claims=EXCLUDED.claims,
                         role=EXCLUDED.role, internal_cost=EXCLUDED.internal_cost,
                         map_price=EXCLUDED.map_price, list_price=EXCLUDED.list_price,
                         inventory_units=EXCLUDED.inventory_units, batch=EXCLUDED.batch,
                         updated_at=now()""",
                    (r["sku"], r["gtin"], r["name"], r["category"], r["description"],
                     json.dumps(r.get("attributes", {})), r.get("currency", "AUD"),
                     json.dumps(r.get("claims", [])), r.get("role", "core"),
                     r["internal_cost"], r["map_price"], r["list_price"],
                     r.get("inventory_units", 0), r.get("batch")),
                )

    # Product-graph edges, inserted after every product exists so the foreign
    # keys resolve regardless of the order the catalog lists them in.
    edges = 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                for related in r.get("complements", []):
                    cur.execute(
                        """INSERT INTO product_relations (sku, related_sku, relation)
                           VALUES (%s, %s, 'complement')
                           ON CONFLICT DO NOTHING""",
                        (r["sku"], related),
                    )
                    edges += 1
    return len(rows), edges


def seed_provenance() -> int:
    rows = load_json("supply_chain_events.json")
    with db.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """INSERT INTO provenance_events
                         (sku, batch, event_type, biz_step, location, actor, event_time, note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (sku, batch, biz_step, event_time) DO NOTHING""",
                    (r["sku"], r["batch"], r["event_type"], r["biz_step"],
                     r["location"], r["actor"], r["ts"], r.get("note")),
                )
    return len(rows)


def seed_features() -> tuple[int, int, list[dict]]:
    """Push commercial features into the online store and run the competitor
    feed through the real outlier filter -- the same code path the live
    ingestion service uses, so the rejected outlier in the demo feed is
    rejected here for the same reason it would be in production."""
    from agentmarket_core.adapters.featurestore import build_feature_store

    store = build_feature_store()
    products = load_json("products.json")
    for p in products:
        store.write(
            p["sku"],
            internal_cost=float(p["internal_cost"]),
            map_price=float(p["map_price"]),
            list_price=float(p["list_price"]),
            inventory_units=int(p.get("inventory_units", 0)),
            batch=p.get("batch"),
        )

    accepted = 0
    rejected: list[dict] = []
    for obs in load_json("competitor_feed.json"):
        ok, reason = store.ingest_competitor_price(obs["sku"], obs["competitor"], float(obs["price"]))
        if ok:
            accepted += 1
        else:
            rejected.append({"sku": obs["sku"], "competitor": obs["competitor"],
                             "price": obs["price"], "reason": reason})
    return len(products), accepted, rejected


def build_index() -> tuple[int, str]:
    embedder = build_embedder()
    rows = db.query(
        "SELECT sku, name, category, description, attributes, claims FROM products ORDER BY sku"
    )
    documents = [
        (r["sku"], document_text(r["name"], r["category"], r["description"],
                                 r["attributes"], r["claims"]))
        for r in rows
    ]
    store = PgVectorStore(embedder=embedder)
    return store.index(documents), embedder.name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reembed", action="store_true", help="only rebuild the vector index")
    parser.add_argument("--reset", action="store_true", help="truncate domain tables before seeding")
    args = parser.parse_args()

    configure_logging()
    print(f"database : {settings.database_url.rsplit('@', 1)[-1]}")
    db.wait_for_postgres()
    db.migrate()

    if args.reset:
        db.execute("TRUNCATE " + ", ".join(TABLES_TO_RESET) + " RESTART IDENTITY CASCADE")
        print("reset    : domain tables truncated")

    if not args.reembed:
        n_products, n_edges = seed_products()
        print(f"products : {n_products} upserted, {n_edges} product-graph edges")
        print(f"provenance: {seed_provenance()} EPCIS events upserted")
        n_feature_skus, accepted, rejected = seed_features()
        print(f"features : {n_feature_skus} SKUs seeded; {accepted} competitor observations accepted")
        for r in rejected:
            print(f"  REJECTED (outlier filter): {r['competitor']} quoted {r['price']} "
                  f"for {r['sku']} -> {r['reason']}")

    indexed, model = build_index()
    print(f"vectors  : {indexed} embeddings indexed with {model}")
    print("\nSeed complete.")


if __name__ == "__main__":
    main()
