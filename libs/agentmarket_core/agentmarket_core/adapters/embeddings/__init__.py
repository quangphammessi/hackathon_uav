"""Embedding port + factory (proposal §5.1, the semantic layer).

Two backends:

  ollama -- real dense embeddings from a locally-served model
            (`ollama pull nomic-embed-text`). Local means no per-token cost
            and no agent query leaving the merchant's network, which is the
            same argument that put the planner LLM on Ollama.
  hash   -- a deterministic hashed character-n-gram embedding computed in
            numpy. No model, no download, no network. It is genuinely weaker
            at synonymy than a trained model, but it is a real vector with
            real cosine geometry, so every downstream component (pgvector
            column, ANN index, similarity ranking) is exercised for real.
            This is what keeps CI and an offline laptop working.

Both emit vectors of exactly `settings.embedding_dimensions` (768), L2-
normalized. Fixing the width at the port -- rather than letting each model
dictate it -- is what allows the `vector(768)` column and its index to stay
put when you change embedding models.
"""
from __future__ import annotations

import abc

from agentmarket_core.config import settings


class Embedder(abc.ABC):
    name: str = "embedder"

    @abc.abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @abc.abstractmethod
    def health(self) -> dict: ...


def build_embedder(backend: str | None = None) -> Embedder:
    """Build the configured embedder.

    `ollama` degrades to `hash` rather than failing: an unreachable model
    server should make search less clever, not take the storefront down. The
    two are not interchangeable at query time though -- vectors written by
    one backend are meaningless to the other -- so the model name is stored
    alongside every embedding and the vector store re-embeds when it changes.
    """
    backend = (backend or settings.embeddings_backend).lower()
    if backend == "ollama":
        from agentmarket_core.adapters.embeddings.ollama_embedder import OllamaEmbedder

        embedder = OllamaEmbedder()
        if embedder.health().get("status") == "ok":
            return embedder
        from agentmarket_core.adapters.embeddings.hashing import HashingEmbedder

        return HashingEmbedder()
    if backend == "hash":
        from agentmarket_core.adapters.embeddings.hashing import HashingEmbedder

        return HashingEmbedder()
    raise ValueError(f"unknown EMBEDDINGS_BACKEND={backend!r}; expected one of: ollama, hash")


__all__ = ["Embedder", "build_embedder"]
