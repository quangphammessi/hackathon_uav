"""Ollama embeddings via /api/embed.

`nomic-embed-text` is 768-dimensional, which is why 768 is the default
`EMBEDDING_DIMENSIONS`. A model of a different width is reconciled to the
configured width rather than being rejected -- truncate if longer, zero-pad
if shorter -- so switching models never requires a schema migration. Both
operations change the vector's meaning slightly; the alternative is a failed
startup or a silently-corrupt column, and this is the lesser evil as long as
you re-embed the catalog (which `scripts/seed.py --reembed` does) after a
model change.
"""
from __future__ import annotations

import logging

import httpx
import numpy as np

from agentmarket_core.adapters.embeddings import Embedder
from agentmarket_core.config import settings

log = logging.getLogger("agentmarket.embeddings.ollama")


class OllamaEmbedder(Embedder):
    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self.base_url = base_url or settings.ollama_base_url
        self.model = model or settings.ollama_embedding_model
        self.name = f"ollama:{self.model}"
        self.dimensions = settings.embedding_dimensions
        self._client = httpx.Client(base_url=self.base_url, timeout=settings.ollama_timeout_seconds)

    def _fit(self, vector: list[float]) -> list[float]:
        arr = np.asarray(vector, dtype=np.float32)
        if arr.shape[0] > self.dimensions:
            arr = arr[: self.dimensions]
        elif arr.shape[0] < self.dimensions:
            arr = np.pad(arr, (0, self.dimensions - arr.shape[0]))
        norm = float(np.linalg.norm(arr))
        if norm > 0:
            arr = arr / norm
        return arr.tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.post("/api/embed", json={"model": self.model, "input": texts})
        resp.raise_for_status()
        body = resp.json()
        vectors = body.get("embeddings")
        if not vectors:
            raise ValueError(f"ollama returned no embeddings for {len(texts)} input(s)")
        return [self._fit(v) for v in vectors]

    def health(self) -> dict:
        try:
            resp = self._client.get("/api/tags", timeout=2.0)
            if resp.status_code != 200:
                return {"backend": "ollama", "status": "degraded", "detail": f"HTTP {resp.status_code}"}
            names = {m.get("name", "").split(":")[0] for m in resp.json().get("models", [])}
            if self.model.split(":")[0] not in names:
                return {
                    "backend": "ollama",
                    "status": "degraded",
                    "detail": f"model {self.model!r} not pulled (run: ollama pull {self.model})",
                }
            return {"backend": "ollama", "status": "ok", "model": self.model, "dimensions": self.dimensions}
        except httpx.HTTPError as exc:
            return {"backend": "ollama", "status": "degraded", "detail": str(exc)}
