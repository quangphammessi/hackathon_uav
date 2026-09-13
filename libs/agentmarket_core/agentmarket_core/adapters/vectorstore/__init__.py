"""Vector store port + factory (proposal §5.1).

  pgvector -- production. Embeddings live in Postgres beside the structured
              product rows, so "semantic match AND in stock AND under $200"
              is one query with one consistency model, instead of a fan-out
              to a separate vector service followed by an application-side
              join that can disagree with itself.
  tfidf    -- scikit-learn TF-IDF + cosine, held in memory. No database.
              Used by unit tests.

Both return `[(sku, score), ...]` ranked best-first, score in [0, 1].
"""
from __future__ import annotations

import abc

from agentmarket_core.config import settings


class VectorStore(abc.ABC):
    @abc.abstractmethod
    def index(self, documents: list[tuple[str, str]]) -> int:
        """Embed and store `(sku, text)` pairs. Returns rows written."""

    @abc.abstractmethod
    def search(self, query: str, top_k: int = 5, max_price: float | None = None) -> list[tuple[str, float]]: ...

    @abc.abstractmethod
    def health(self) -> dict: ...


def build_vector_store(backend: str | None = None, embedder=None) -> VectorStore:
    backend = (backend or settings.vector_store_backend).lower()
    if backend == "pgvector":
        from agentmarket_core.adapters.vectorstore.pgvector_store import PgVectorStore

        return PgVectorStore(embedder=embedder)
    if backend == "tfidf":
        from agentmarket_core.adapters.vectorstore.tfidf_store import TfidfVectorStore

        return TfidfVectorStore()
    raise ValueError(f"unknown VECTOR_STORE_BACKEND={backend!r}; expected one of: pgvector, tfidf")


__all__ = ["VectorStore", "build_vector_store"]
