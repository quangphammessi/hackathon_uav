"""Postgres-backed product store."""
from __future__ import annotations

from agentmarket_core import db
from agentmarket_core.adapters.productstore import ProductStore, document_text
from agentmarket_core.models import ProductSpec

# Columns safe to hand to an agent. Enumerated explicitly rather than
# SELECT * so that adding a commercial column to the table can never
# accidentally widen the public view.
PUBLIC_COLUMNS = "sku, gtin, name, category, description, attributes, currency, claims, role"


class PostgresProductStore(ProductStore):
    def get(self, sku: str) -> ProductSpec | None:
        row = db.query_one(f"SELECT {PUBLIC_COLUMNS} FROM products WHERE sku = %s", (sku,))
        return ProductSpec(**row) if row else None

    def get_public_pricing(self, sku: str) -> dict | None:
        return db.query_one(
            """SELECT sku, currency, list_price::float AS list_price, inventory_units,
                      (inventory_units > 0) AS in_stock
                 FROM products WHERE sku = %s""",
            (sku,),
        )

    def structured_candidates(
        self,
        max_price: float | None = None,
        experience_levels: list[str] | None = None,
        use_cases: list[str] | None = None,
        claims: list[str] | None = None,
        product_types: list[str] | None = None,
        limit: int = 12,
    ) -> list[str]:
        rows = db.query(
            """
            SELECT sku FROM products
             WHERE inventory_units > 0
               AND (%(max_price)s::numeric IS NULL OR list_price <= %(max_price)s::numeric)
               AND (%(levels)s::text[] IS NULL
                    OR attributes->>'experience_level' = ANY(%(levels)s::text[]))
               AND (%(uses)s::text[] IS NULL OR attributes->'use_cases' ?| %(uses)s::text[])
               AND (%(claims)s::text[] IS NULL OR claims ?| %(claims)s::text[])
               AND (%(types)s::text[] IS NULL
                    OR attributes->>'product_type' = ANY(%(types)s::text[]))
             ORDER BY list_price
             LIMIT %(limit)s
            """,
            {
                "max_price": max_price,
                "levels": experience_levels or None,
                "uses": use_cases or None,
                "claims": claims or None,
                "types": product_types or None,
                "limit": limit,
            },
        )
        return [r["sku"] for r in rows]

    def complements_of(self, sku: str) -> list[str]:
        rows = db.query(
            "SELECT related_sku FROM product_relations WHERE sku = %s AND relation = 'complement'",
            (sku,),
        )
        return [r["related_sku"] for r in rows]

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
        rows = db.query(
            "SELECT sku, name, category, description, attributes, claims FROM products ORDER BY sku"
        )
        return [
            (r["sku"], document_text(r["name"], r["category"], r["description"],
                                     r["attributes"], r["claims"]))
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
