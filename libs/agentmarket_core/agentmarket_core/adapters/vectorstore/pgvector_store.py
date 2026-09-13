"""pgvector-backed semantic search.

The reason this is worth doing in Postgres rather than a dedicated vector
service: an agent's request is almost never purely semantic. "A waterproof
hiking boot under $200 that's actually in stock" is a similarity ranking
*and* two hard predicates. Here that is a single SQL statement with a join to
`products` -- one round trip, one snapshot, no chance of the vector index and
the inventory table disagreeing about what exists. With a separate vector DB
you get the nearest 50 by vector, then filter them in the application, and
discover that all 50 were out of stock and you now have nothing to show.

`<=>` is pgvector's cosine *distance* operator (0 = identical, 2 = opposite).
Similarity is reported as `1 - distance` so scores rise with relevance, which
is what every caller expects.
"""
from __future__ import annotations

import logging

from agentmarket_core import db
from agentmarket_core.adapters.embeddings import build_embedder
from agentmarket_core.adapters.vectorstore import VectorStore

log = logging.getLogger("agentmarket.vectorstore.pgvector")


def _to_literal(vector: list[float]) -> str:
    """pgvector accepts its text input form: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"


class PgVectorStore(VectorStore):
    def __init__(self, embedder=None) -> None:
        self.embedder = embedder or build_embedder()

    def index(self, documents: list[tuple[str, str]]) -> int:
        if not documents:
            return 0
        skus = [sku for sku, _ in documents]
        vectors = self.embedder.embed([text for _, text in documents])
        written = 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                for sku, vector in zip(skus, vectors):
                    cur.execute(
                        """INSERT INTO product_embeddings (sku, model, embedding, updated_at)
                           VALUES (%s, %s, %s::vector, now())
                           ON CONFLICT (sku) DO UPDATE
                             SET embedding = EXCLUDED.embedding,
                                 model     = EXCLUDED.model,
                                 updated_at = now()""",
                        (sku, self.embedder.name, _to_literal(vector)),
                    )
                    written += 1
        log.info("indexed %d product embeddings with %s", written, self.embedder.name)
        return written

    def search(self, query: str, top_k: int = 5, max_price: float | None = None) -> list[tuple[str, float]]:
        vector = _to_literal(self.embedder.embed_one(query))
        # The price predicate uses list_price as the public-facing ceiling.
        # It is a pre-filter, not a post-filter, precisely so top_k is top_k
        # of the *eligible* set rather than of the whole catalog.
        sql = """
            SELECT p.sku,
                   1 - (e.embedding <=> %(vec)s::vector) AS similarity
              FROM product_embeddings e
              JOIN products p ON p.sku = e.sku
             WHERE e.model = %(model)s
               AND p.inventory_units > 0
               AND (%(max_price)s::numeric IS NULL OR p.list_price <= %(max_price)s::numeric)
             ORDER BY e.embedding <=> %(vec)s::vector
             LIMIT %(k)s
        """
        rows = db.query(sql, {"vec": vector, "model": self.embedder.name,
                              "max_price": max_price, "k": top_k})
        return [(r["sku"], float(r["similarity"])) for r in rows]

    def health(self) -> dict:
        try:
            row = db.query_one(
                "SELECT count(*) AS n, count(DISTINCT model) AS models FROM product_embeddings"
            )
            emb = self.embedder.health()
            status = "ok" if (row and row["n"] > 0) else "degraded"
            return {
                "backend": "pgvector",
                "status": status,
                "indexed": (row or {}).get("n", 0),
                "embedder": emb.get("model", self.embedder.name),
                "detail": None if status == "ok" else "no embeddings indexed yet (run scripts/seed.py)",
            }
        except Exception as exc:  # noqa: BLE001
            return {"backend": "pgvector", "status": "degraded", "detail": str(exc)}
