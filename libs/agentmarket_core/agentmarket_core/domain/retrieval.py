"""Hybrid retrieval: semantic recall plus a structured leg that cannot miss.

A pure vector search is the wrong tool for the last mile of an agent's
request, and the reason is visible in the demo catalog. Asked for gear for
someone who has "never hiked before", the embedding ranks a first-aid kit and
an insulated mat above the one shoe in the catalog whose description is
literally "designed for first-time hikers", because "start hiking" and
"first-time hikers" share no tokens and small embedding models do not reliably
bridge that. The product that satisfies every stated requirement never reaches
the scorer, and the merchant loses a sale it should have won without anything
appearing to go wrong.

So the retriever runs two legs and takes the union. The semantic leg finds
what nobody wrote a rule for. The structured leg asks the catalog the question
the buyer actually asked -- experience level, use case, asserted values, price
ceiling, in stock -- and by construction returns every row that matches it.
Together they miss neither the unexpected product nor the obvious one.

This is also the architecture the brief points at when it says agents "rely on
structured data, API accessibility, deterministic facts". Retrieval that is
purely statistical is not deterministic, and a merchant cannot tell a buyer's
agent why a product was not shown.
"""
from __future__ import annotations

from agentmarket_core.config import settings
from agentmarket_core.domain.intent import EXPERIENCE_SCALE
from agentmarket_core.models import IntentPlan

# Candidates are filtered on list price, but the quote usually lands below
# list, so the ceiling carries slack. The real budget check runs on the quote.
RETRIEVAL_BUDGET_SLACK = 1.3


def allowed_experience_levels(level: str | None) -> list[str]:
    """A beginner may be sold beginner gear; an expert may be sold anything.

    The asymmetry is the point. Selling expert kit to a first-timer is how
    someone ends up cold, blistered and never hiking again, which is a worse
    outcome for the merchant than the lost margin on the cheaper product.
    """
    if not level:
        return []
    ceiling = EXPERIENCE_SCALE.get(level.lower())
    if ceiling is None:
        return []
    return [name for name, rank in EXPERIENCE_SCALE.items() if rank <= ceiling]


def hybrid_candidates(
    plan: IntentPlan,
    product_store,
    vector_store,
    top_k: int | None = None,
) -> list[tuple[str, float, str]]:
    """Returns `(sku, similarity, source)` with `source` in {semantic, structured, both}."""
    top_k = top_k or settings.retrieval_top_k
    price_ceiling = None
    if plan.budget and plan.budget_is_hard:
        price_ceiling = plan.budget * RETRIEVAL_BUDGET_SLACK

    semantic: list[tuple[str, float]] = []
    try:
        semantic = vector_store.search(plan.search_query, top_k=top_k, max_price=price_ceiling)
    except Exception:  # noqa: BLE001 -- a vector-store outage degrades recall, it does not fail the request
        semantic = []

    # Only *hard* predicates filter here. A preference must never remove a row
    # from consideration -- "he gets cold easily" should rank an insulated mat
    # up, not delete every product that has no thermal rating, which is what a
    # filter built from soft constraints would do.
    hard = plan.hard_constraints
    hard_fields = {c.field for c in hard}
    product_types = [str(v) for c in hard if c.field == "product_type"
                     for v in (c.value if isinstance(c.value, (list, tuple)) else [c.value])]
    hard_uses = [str(c.value) for c in hard if c.field == "use_cases"]
    hard_claims = [str(c.value) for c in hard if c.field == "claim"]
    levels = (allowed_experience_levels(plan.experience_level)
              if "experience_level" in hard_fields else [])

    structured: list[str] = []
    try:
        structured = product_store.structured_candidates(
            max_price=price_ceiling,
            experience_levels=levels,
            use_cases=hard_uses or None,
            claims=hard_claims or None,
            product_types=product_types or None,
            limit=top_k,
        )
    except Exception:  # noqa: BLE001
        structured = []

    scores = {sku: score for sku, score in semantic}
    merged: list[tuple[str, float, str]] = []
    for sku, score in semantic:
        merged.append((sku, score, "both" if sku in structured else "semantic"))
    for sku in structured:
        if sku not in scores:
            merged.append((sku, 0.0, "structured"))

    excluded = set(plan.excluded_skus)
    return [item for item in merged if item[0] not in excluded]
