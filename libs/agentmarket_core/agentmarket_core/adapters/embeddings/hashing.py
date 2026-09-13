"""Deterministic hashed-n-gram embeddings -- the no-dependency fallback.

This is the "hashing trick": project character n-grams into a fixed-width
space with a stable hash, accumulate, then L2-normalize. It gives real vector
geometry (cosine similarity is meaningful, near-duplicates land near each
other, the pgvector index behaves exactly as it will in production) without
any model weights.

What it is not: semantically smart. It matches on shared substrings, so
"waterproof boot" and "水 resistant footwear" are unrelated to it in a way
they would not be to a trained model. That is the honest trade -- it keeps
the system runnable and testable offline, and `EMBEDDINGS_BACKEND=ollama`
is the one-line switch to real semantics.

Stability matters: Python's built-in `hash()` is randomized per process
(PYTHONHASHSEED), so using it would mean vectors written by one process
being incomparable to those written by the next. blake2b is stable across
processes and machines.
"""
from __future__ import annotations

import hashlib
import re

import numpy as np

from agentmarket_core.adapters.embeddings import Embedder
from agentmarket_core.config import settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _stable_hash(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


class HashingEmbedder(Embedder):
    name = "hash-ngram-v1"

    def __init__(self, dimensions: int | None = None, ngram_range: tuple[int, int] = (3, 5)) -> None:
        self.dimensions = dimensions or settings.embedding_dimensions
        self.ngram_range = ngram_range

    def _features(self, text: str) -> list[str]:
        tokens = _TOKEN_RE.findall(text.lower())
        features: list[str] = list(tokens)
        # Word bigrams capture short phrases ("water resistant").
        features += [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        # Character n-grams give partial-match robustness (plurals, typos).
        lo, hi = self.ngram_range
        for token in tokens:
            padded = f" {token} "
            for n in range(lo, hi + 1):
                features += [padded[i : i + n] for i in range(len(padded) - n + 1)]
        return features

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = np.zeros(self.dimensions, dtype=np.float32)
            for feature in self._features(text):
                h = _stable_hash(feature)
                index = h % self.dimensions
                # Signed accumulation: without the sign, every feature adds
                # positively and unrelated documents drift toward a common
                # direction, flattening the similarity scale.
                sign = 1.0 if (h >> 63) & 1 else -1.0
                vec[index] += sign
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec /= norm
            out.append(vec.tolist())
        return out

    def health(self) -> dict:
        return {"backend": "hash", "status": "ok", "model": self.name, "dimensions": self.dimensions}
