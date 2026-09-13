"""Storefront service -- Subsystem 2 (proposal §5).

Runs the LangGraph workflow. Holds no durable state of its own beyond the
vector index: the product facts, the prices and the trust verdicts all come
from the services that own them, which is what keeps the Offer contract
honest under §5.4.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from agentmarket_core import llm
from agentmarket_core.adapters.bus import EventBus
from agentmarket_core.adapters.productstore import build_product_store
from agentmarket_core.adapters.vectorstore import build_vector_store
from agentmarket_core.clients import PricingClient, VerificationClient
from agentmarket_core.service import create_app
from agentmarket_core.tracing import Tracer

from graph import StorefrontGraph

log = logging.getLogger("agentmarket.storefront.api")

product_store = build_product_store()
vector_store = build_vector_store()
pricing_client = PricingClient()
verification_client = VerificationClient()
graph: StorefrontGraph | None = None


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    query: str


def _startup(bus: EventBus) -> None:
    global graph
    graph = StorefrontGraph(
        product_store=product_store,
        vector_store=vector_store,
        tracer=Tracer(bus=bus, service="storefront"),
        pricing=pricing_client,
        verification=verification_client,
        bus=bus,
    )


def _llm_probe() -> dict:
    ok = llm.is_ollama_available()
    return {"llm": {
        "backend": "ollama" if ok else "heuristic-fallback",
        # A missing model server is a *degraded* storefront, not a broken one:
        # planning falls back to the keyword parser and everything downstream
        # still works. Reporting "ok" here would hide a real quality drop.
        "status": "ok" if ok else "degraded",
        "detail": None if ok else "Ollama unreachable; planner/repair using keyword fallback",
    }}


app, ctx = create_app(
    "storefront",
    on_startup=_startup,
    health_probes=lambda: {
        "vector_store": vector_store.health(),
        "product_store": product_store.health(),
        "pricing": pricing_client.health(),
        "verification": verification_client.health(),
        **_llm_probe(),
    },
)


@app.post("/v1/query", tags=["storefront"])
def query(req: QueryRequest) -> dict:
    if not req.query.strip():
        raise HTTPException(status_code=422, detail="query must not be empty")
    return graph.run(agent_id=req.agent_id, query=req.query)


@app.get("/v1/catalog", tags=["storefront"])
def catalog() -> dict:
    """The agent-facing catalog. Commercial fields are structurally absent --
    `ProductSpec` has nowhere to put them."""
    return {"products": [s.model_dump() for s in product_store.all_specs()]}


@app.get("/v1/catalog/{sku}", tags=["storefront"])
def catalog_item(sku: str) -> dict:
    spec = product_store.get(sku)
    if not spec:
        raise HTTPException(status_code=404, detail="unknown sku")
    return spec.model_dump()


@app.post("/v1/reindex", tags=["storefront"])
def reindex() -> dict:
    """Rebuild the vector index from the product store -- run after a catalog
    change or an embedding-model switch."""
    count = vector_store.index(product_store.searchable_documents())
    return {"indexed": count}
