"""JSON-file product store. Test/offline backend, reads data/products.json."""
from __future__ import annotations

import json

from agentmarket_core.adapters.productstore import ProductStore, document_text
from agentmarket_core.config import settings
from agentmarket_core.models import ProductSpec

PUBLIC_FIELDS = ("sku", "gtin", "name", "category", "description", "attributes", "currency")


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
            (r["sku"], document_text(r["name"], r["category"], r["description"], r.get("attributes", {})))
            for r in (self._rows[sku] for sku in sorted(self._rows))
        ]

    def health(self) -> dict:
        return {"backend": "json", "status": "ok", "products": len(self._rows)}
