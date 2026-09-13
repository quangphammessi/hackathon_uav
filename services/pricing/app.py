"""Pricing service -- Subsystem 1 (proposal §4).

Owns the Quote API and the competitor-signal consumer. Two responsibilities,
one process, because they share the feature store and the bandit: the
consumer writes the features the Quote API reads, and coupling them through
a network hop would buy nothing.

The consumer is what makes pricing *reactive* rather than batch. A competitor
price change published to `market.competitor_signal` moves our fair value on
the next quote, with no polling and no scheduled job.
"""
from __future__ import annotations

import logging

from fastapi import Body, HTTPException
from pydantic import BaseModel, ConfigDict

from agentmarket_core.adapters.bus import EventBus
from agentmarket_core.adapters.featurestore import build_feature_store
from agentmarket_core.adapters.productstore import build_product_store
from agentmarket_core.config import TOPIC_COMPETITOR_SIGNAL, settings
from agentmarket_core.domain.bandit import PersistentBandit
from agentmarket_core.domain.pricing import PricingEngine
from agentmarket_core.models import PriceQuote
from agentmarket_core.service import create_app

log = logging.getLogger("agentmarket.pricing.api")

feature_store = build_feature_store()
product_store = build_product_store()
bandit = PersistentBandit()
engine: PricingEngine | None = None


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str
    sku: str
    amount: float


class OutcomeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str
    won: bool


class CompetitorPriceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str
    competitor: str
    price: float


class BundleQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skus: list[str]


class ConcessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str
    opening_amount: float
    target_amount: float


def _on_competitor_signal(envelope: dict) -> None:
    """Ingest a competitor observation published by anything upstream --
    a scraper, a partner feed, a manual correction."""
    p = envelope.get("payload", {})
    sku, competitor, price = p.get("sku"), p.get("competitor"), p.get("price")
    if not sku or competitor is None or price is None:
        return
    accepted, reason = feature_store.ingest_competitor_price(sku, competitor, float(price))
    log.info("competitor signal ingested", extra={
        "sku": sku, "competitor": competitor, "price": price,
        "accepted": accepted, "reason": reason,
    })


def _startup(bus: EventBus) -> None:
    global engine
    engine = PricingEngine(store=feature_store, bus=bus, bandit=bandit)
    bus.subscribe(TOPIC_COMPETITOR_SIGNAL, _on_competitor_signal)


app, ctx = create_app(
    "pricing",
    on_startup=_startup,
    health_probes=lambda: {"feature_store": feature_store.health(),
                           "product_store": product_store.health()},
)


@app.post("/v1/quote", response_model=PriceQuote, tags=["pricing"])
def create_quote(req: QuoteRequest) -> PriceQuote:
    try:
        return engine.quote(req.sku)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/quote/validate", tags=["pricing"])
def validate_quote(req: ValidateRequest) -> dict:
    return {"valid": engine.is_quote_valid(req.quote_id, req.sku, req.amount)}


@app.post("/v1/quote/outcome", tags=["pricing"])
def record_outcome(req: OutcomeRequest) -> dict:
    engine.record_outcome(req.quote_id, req.won)
    return {"recorded": True}


@app.post("/v1/quote/bundle", tags=["pricing"])
def quote_bundle(req: BundleQuoteRequest) -> dict:
    """Price a kit as a unit.

    Lives here rather than in the storefront because the discount is bounded
    by each component's own floor, and those floors are computed from cost and
    MAP. The storefront chooses what goes in the kit; it is structurally
    unable to decide what the kit may cost.
    """
    if not req.skus:
        raise HTTPException(status_code=400, detail="skus must not be empty")
    try:
        return engine.quote_bundle(req.skus)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/quote/concession", tags=["pricing"])
def quote_concession(req: ConcessionRequest) -> dict:
    """Answer a buyer agent's counter-offer on one SKU.

    The response carries a price, an outcome and a reason code. It never
    carries cost, MAP or margin: `FLOOR_REACHED` is the whole explanation a
    counterparty is entitled to, and it is enough for the buyer's agent to
    stop pushing and decide.
    """
    try:
        decision, quote = engine.concede(req.sku, req.opening_amount, req.target_amount)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {**decision.to_dict(), "quote": quote.model_dump() if quote else None}


@app.get("/v1/quote/{quote_id}", response_model=PriceQuote, tags=["pricing"])
def get_quote(quote_id: str) -> PriceQuote:
    quote = engine.get_quote(quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail="quote not found")
    return quote


@app.post("/v1/competitor-price", tags=["pricing"])
def ingest_competitor_price(req: CompetitorPriceRequest) -> dict:
    """Synchronous ingest path, for feeds that push over HTTP rather than
    publishing to the bus. Runs the identical outlier filter."""
    accepted, reason = feature_store.ingest_competitor_price(req.sku, req.competitor, req.price)
    ctx["bus"].publish(TOPIC_COMPETITOR_SIGNAL, {
        "sku": req.sku, "competitor": req.competitor, "price": req.price,
        "accepted": accepted, "reason": reason,
    })
    return {"accepted": accepted, "reason": reason}


@app.get("/v1/market/{sku}", tags=["pricing"])
def market_view(sku: str) -> dict:
    """Everything the Ops Dashboard needs to explain one SKU's price:
    what the market is doing, what we would quote, and what the bandit has
    learned so far."""
    commercial = product_store.get_commercial(sku)
    if not commercial:
        raise HTTPException(status_code=404, detail="unknown sku")
    spec = product_store.get(sku)
    features = feature_store.get_online(sku)
    detail = feature_store.competitor_detail(sku) if hasattr(feature_store, "competitor_detail") else []
    try:
        fair_value, cost_floor = engine._fair_value(sku)  # noqa: SLF001 -- introspection endpoint
    except KeyError:
        fair_value, cost_floor = None, None
    return {
        "sku": sku,
        "name": spec.name if spec else sku,
        "competitors": sorted(detail, key=lambda d: d["price"]),
        "competitor_prices": features.get("competitor_prices", []),
        "map_price": commercial["map_price"],
        "list_price": commercial["list_price"],
        "inventory_units": commercial["inventory_units"],
        "fair_value": round(fair_value, 2) if fair_value else None,
        "margin_floor": round(cost_floor, 2) if cost_floor else None,
        "bandit_arms": bandit.snapshot(sku),
    }


@app.get("/v1/market", tags=["pricing"])
def market_overview() -> dict:
    rows = product_store.all_commercial() if hasattr(product_store, "all_commercial") else []
    out = []
    for row in rows:
        features = feature_store.get_online(row["sku"])
        prices = features.get("competitor_prices") or []
        out.append({
            "sku": row["sku"], "name": row["name"],
            "list_price": row["list_price"], "map_price": row["map_price"],
            "inventory_units": row["inventory_units"],
            "competitor_count": len(prices),
            "competitor_min": min(prices) if prices else None,
            "competitor_max": max(prices) if prices else None,
        })
    return {"skus": out}
