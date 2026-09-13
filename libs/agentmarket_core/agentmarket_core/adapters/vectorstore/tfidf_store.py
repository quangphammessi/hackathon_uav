"""TF-IDF + cosine vector store, held in memory. Test/offline backend.

No database, no embedding model, no network -- which is exactly what a unit
test wants. It does not support the price pre-filter in SQL, so it applies
the predicate in Python against whatever catalog it was indexed with.
"""
from __future__ import annotations

from agentmarket_core.adapters.vectorstore import VectorStore


class TfidfVectorStore(VectorStore):
    def __init__(self) -> None:
        self._skus: list[str] = []
        self._matrix = None
        self._vectorizer = None
        self._prices: dict[str, float] = {}

    def index(self, documents: list[tuple[str, str]], prices: dict[str, float] | None = None) -> int:
        from sklearn.feature_extraction.text import TfidfVectorizer

        if not documents:
            return 0
        self._skus = [sku for sku, _ in documents]
        self._prices = prices or {}
        self._vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
        self._matrix = self._vectorizer.fit_transform([text for _, text in documents])
        return len(self._skus)

    def search(self, query: str, top_k: int = 5, max_price: float | None = None) -> list[tuple[str, float]]:
        if self._matrix is None or not self._skus:
            return []
        from sklearn.metrics.pairwise import cosine_similarity

        scores = cosine_similarity(self._vectorizer.transform([query]), self._matrix)[0]
        ranked = sorted(zip(self._skus, scores), key=lambda pair: pair[1], reverse=True)
        if max_price is not None and self._prices:
            ranked = [(s, sc) for s, sc in ranked if self._prices.get(s, 0.0) <= max_price]
        return [(sku, float(score)) for sku, score in ranked[:top_k] if score > 0]

    def health(self) -> dict:
        return {"backend": "tfidf", "status": "ok", "indexed": len(self._skus)}
