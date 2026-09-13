"""Structured product store (proposal §5.1) -- the factual source of truth.

The split that matters here is `get()` vs `get_commercial()`:

* `get()` returns a `ProductSpec` -- the public, agent-safe view.
* `get_commercial()` returns internal cost, MAP and inventory, and is only
  ever called by the pricing engine inside the trust boundary.

Two methods rather than one object with optional fields, because "remember to
strip the cost field before serializing" is a rule that gets forgotten
exactly once, and leaking unit economics to a buyer's agent is not a
recoverable mistake. The type system enforces it instead: `ProductSpec` has
nowhere to put a cost.
"""
from __future__ import annotations

import abc

from agentmarket_core.config import settings
from agentmarket_core.models import ProductSpec


class ProductStore(abc.ABC):
    @abc.abstractmethod
    def get(self, sku: str) -> ProductSpec | None: ...

    @abc.abstractmethod
    def get_commercial(self, sku: str) -> dict | None: ...

    @abc.abstractmethod
    def all_specs(self) -> list[ProductSpec]: ...

    @abc.abstractmethod
    def searchable_documents(self) -> list[tuple[str, str]]:
        """`(sku, text)` pairs for the vector store to embed."""

    @abc.abstractmethod
    def health(self) -> dict: ...


def build_product_store(backend: str | None = None) -> ProductStore:
    backend = (backend or settings.product_store_backend).lower()
    if backend == "postgres":
        from agentmarket_core.adapters.productstore.postgres_store import PostgresProductStore

        return PostgresProductStore()
    if backend == "json":
        from agentmarket_core.adapters.productstore.json_store import JsonProductStore

        return JsonProductStore()
    raise ValueError(f"unknown PRODUCT_STORE_BACKEND={backend!r}; expected one of: postgres, json")


def document_text(name: str, category: str, description: str, attributes: dict) -> str:
    """Build the text that represents a product to the semantic layer.

    Attributes are flattened into the text rather than left as structured
    fields, because an agent asking for "waterproof" is matching against a
    value (`waterproof: true`) that only becomes searchable once it is
    rendered as words.
    """
    attr_text = " ".join(f"{k} {v}" for k, v in (attributes or {}).items())
    return f"{name}. {category}. {description} {attr_text}".strip()


__all__ = ["ProductStore", "build_product_store", "document_text"]
