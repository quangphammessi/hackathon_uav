"""Postgres-backed product store."""
from __future__ import annotations

from agentmarket_core import db
from agentmarket_core.adapters.productstore import ProductStore, document_text
from agentmarket_core.models import ProductSpec

# Columns safe to hand to an agent. Enumerated explicitly rather than
# SELECT * so that adding a commercial column to the table can never
# accidentally widen the public view.
PUBLIC_COLUMNS = "sku, gtin, name, category, description, attributes, currency"


class PostgresProductStore(ProductStore):
    def get(self, sku: str) -> ProductSpec | None:
        row = db.query_one(f"SELECT {PUBLIC_COLUMNS} FROM products WHERE sku = %s", (sku,))
        return ProductSpec(**row) if row else None

    def get_commercial(self, sku: str) -> dict | None:
        return db.query_one(
            """SELECT sku, gtin, batch,
                      internal_cost::float   AS internal_cost,
                      map_price::float       AS map_price,
                      list_price::float      AS list_price,
                      inventory_units
                 FROM products WHERE sku = %s""",
            (sku,),
        )

    def all_specs(self) -> list[ProductSpec]:
        rows = db.query(f"SELECT {PUBLIC_COLUMNS} FROM products ORDER BY sku")
        return [ProductSpec(**r) for r in rows]

    def all_commercial(self) -> list[dict]:
        return db.query(
            """SELECT sku, name, batch,
                      internal_cost::float AS internal_cost,
                      map_price::float     AS map_price,
                      list_price::float    AS list_price,
                      inventory_units
                 FROM products ORDER BY sku"""
        )

    def searchable_documents(self) -> list[tuple[str, str]]:
        rows = db.query("SELECT sku, name, category, description, attributes FROM products ORDER BY sku")
        return [
            (r["sku"], document_text(r["name"], r["category"], r["description"], r["attributes"]))
            for r in rows
        ]

    def health(self) -> dict:
        try:
            row = db.query_one("SELECT count(*) AS n FROM products")
            n = (row or {}).get("n", 0)
            return {
                "backend": "postgres",
                "status": "ok" if n else "degraded",
                "products": n,
                "detail": None if n else "catalog empty (run scripts/seed.py)",
            }
        except Exception as exc:  # noqa: BLE001
            return {"backend": "postgres", "status": "degraded", "detail": str(exc)}
