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
    def get_public_pricing(self, sku: str) -> dict | None:
        """List price, currency and availability -- the pricing facts a buyer
        could read off a shelf edge.

        Separate from `get_commercial` so the storefront can reason about
        affordability without ever holding cost or MAP. Before this split the
        storefront called `get_commercial` and simply chose not to use the
        confidential fields, which is a convention; this is a boundary.
        """

    @abc.abstractmethod
    def structured_candidates(
        self,
        max_price: float | None = None,
        experience_levels: list[str] | None = None,
        use_cases: list[str] | None = None,
        claims: list[str] | None = None,
        product_types: list[str] | None = None,
        limit: int = 12,
    ) -> list[str]:
        """Candidates that satisfy the decoded predicates, found by query
        rather than by similarity.

        The semantic leg and this one fail differently, which is why the
        retriever runs both. An embedding search can miss a product that
        satisfies every stated requirement -- "start hiking" and "first-time
        hikers" are the same need and not the same tokens, and the gap widens
        when the embedding model is small or the catalog copy is terse. A
        structured query cannot miss it, because it is asking the question the
        buyer actually asked. Conversely the structured leg only knows the
        predicates that were decoded, and the embedding is what surfaces the
        product nobody thought to write a rule for. The union has the
        weaknesses of neither.
        """

    @abc.abstractmethod
    def complements_of(self, sku: str) -> list[str]:
        """SKUs that go with this one, from the product graph.

        Merchandising knowledge, not similarity: a hydration bladder is not
        semantically close to a backpack, it is complementary to one, and no
        embedding recovers that.
        """

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


def document_text(name: str, category: str, description: str, attributes: dict,
                  claims: list[str] | None = None) -> str:
    """Build the text that represents a product to the semantic layer.

    Attributes are flattened into the text rather than left as structured
    fields, because an agent asking for "waterproof" is matching against a
    value (`waterproof: true`) that only becomes searchable once it is
    rendered as words.

    Asserted claims are included too, and deliberately so even though they are
    unverified at this stage. Retrieval is a recall step: a product that
    *claims* to be ethically made should surface for a buyer who asked for
    that, and then be held to account by the attestation check downstream. A
    greenwashed product that never gets retrieved never gets caught.
    """
    attr_text = " ".join(f"{k} {v}" for k, v in (attributes or {}).items())
    claim_text = " ".join(str(c).replace("_", " ") for c in (claims or []))
    return f"{name}. {category}. {description} {attr_text} {claim_text}".strip()


__all__ = ["ProductStore", "build_product_store", "document_text"]
