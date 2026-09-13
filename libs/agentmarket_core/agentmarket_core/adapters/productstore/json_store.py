"""JSON-file product store. Test/offline backend, reads data/products.json."""
from __future__ import annotations

import json

from agentmarket_core.adapters.productstore import ProductStore, document_text
from agentmarket_core.config import settings
from agentmarket_core.models import ProductSpec

PUBLIC_FIELDS = ("sku", "gtin", "name", "category", "description", "attributes",
                 "currency", "claims", "role")


class JsonProductStore(ProductStore):
    def __init__(self, path=None) -> None:
        self.path = path or (settings.data_dir / "products.json")
        raw = json.loads(self.path.read_text())
        self._rows: dict[str, dict] = {r["sku"]: r for r in raw}

    def get(self, sku: str) -> ProductSpec | None:
        row = self._rows.get(sku)
        if not row:
            return None
        return ProductSpec(**{k: row[k] for k in PUBLIC_FIELDS if k in row})

    def get_public_pricing(self, sku: str) -> dict | None:
        row = self._rows.get(sku)
        if not row:
            return None
        units = int(row.get("inventory_units", 0))
        return {
            "sku": row["sku"], "currency": row.get("currency", "AUD"),
            "list_price": float(row["list_price"]),
            "inventory_units": units, "in_stock": units > 0,
        }

    def structured_candidates(
        self,
        max_price: float | None = None,
        experience_levels: list[str] | None = None,
        use_cases: list[str] | None = None,
        claims: list[str] | None = None,
        product_types: list[str] | None = None,
        limit: int = 12,
    ) -> list[str]:
        wanted_types = set(product_types or [])
        wanted_levels = set(experience_levels or [])
        wanted_uses = set(use_cases or [])
        wanted_claims = set(claims or [])
        out: list[tuple[float, str]] = []
        for sku, row in self._rows.items():
            if int(row.get("inventory_units", 0)) <= 0:
                continue
            price = float(row["list_price"])
            if max_price is not None and price > max_price:
                continue
            attributes = row.get("attributes", {})
            if wanted_levels and attributes.get("experience_level") not in wanted_levels:
                continue
            if wanted_uses and not (wanted_uses & set(attributes.get("use_cases", []))):
                continue
            if wanted_claims and not (wanted_claims & set(row.get("claims", []))):
                continue
            if wanted_types and attributes.get("product_type") not in wanted_types:
                continue
            out.append((price, sku))
        return [sku for _, sku in sorted(out)][:limit]

    def complements_of(self, sku: str) -> list[str]:
        row = self._rows.get(sku) or {}
        return [s for s in row.get("complements", []) if s in self._rows]

    def get_commercial(self, sku: str) -> dict | None:
        row = self._rows.get(sku)
        if not row:
            return None
        return {
            "sku": row["sku"], "gtin": row["gtin"], "batch": row.get("batch"),
            "internal_cost": float(row["internal_cost"]),
            "map_price": float(row["map_price"]),
            "list_price": float(row["list_price"]),
            "inventory_units": int(row.get("inventory_units", 0)),
        }

    def all_specs(self) -> list[ProductSpec]:
        return [s for s in (self.get(sku) for sku in sorted(self._rows)) if s is not None]

    def all_commercial(self) -> list[dict]:
        return [c for c in (self.get_commercial(sku) for sku in sorted(self._rows)) if c is not None]

    def searchable_documents(self) -> list[tuple[str, str]]:
        return [
            (r["sku"], document_text(r["name"], r["category"], r["description"],
                                     r.get("attributes", {}), r.get("claims", [])))
            for r in (self._rows[sku] for sku in sorted(self._rows))
        ]

    def health(self) -> dict:
        return {"backend": "json", "status": "ok", "products": len(self._rows)}
