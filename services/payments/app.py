"""Payments service -- mandate signing and settlement (proposal §6.4)."""
from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from agentmarket_core.adapters.bus import EventBus
from agentmarket_core.clients import PricingClient, VerificationClient
from agentmarket_core.domain.mandates import MandateService
from agentmarket_core.domain.payments import PaymentOrchestrator
from agentmarket_core.models import CartItem, CartMandate, IntentMandate, OrderResult
from agentmarket_core.service import create_app
from agentmarket_core import db

log = logging.getLogger("agentmarket.payments.api")

mandate_service = MandateService()
pricing_client = PricingClient()
verification_client = VerificationClient()
orchestrator: PaymentOrchestrator | None = None


class IntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_id: str
    agent_id: str
    instructions: str
    max_amount: float


class CartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_id: str
    intent_mandate: IntentMandate
    sku: str
    quote_id: str
    amount: float
    trust_token_ref: str
    items: list[CartItem] = []
    bundle_id: str | None = None


class PayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_id: str
    intent_mandate: IntentMandate
    cart_mandate: CartMandate


def _startup(bus: EventBus) -> None:
    global orchestrator
    orchestrator = PaymentOrchestrator(
        mandates=mandate_service,
        pricing_client=pricing_client,
        verification_client=verification_client,
        bus=bus,
    )


app, ctx = create_app(
    "payments",
    on_startup=_startup,
    health_probes=lambda: {
        "pricing": pricing_client.health(),
        "verification": verification_client.health(),
    },
)


@app.post("/v1/mandates/intent", response_model=IntentMandate, tags=["payments"])
def create_intent(req: IntentRequest) -> IntentMandate:
    """Stands in for the principal's wallet signing an Intent Mandate. In
    production the private key never reaches this service -- it would receive
    an already-signed mandate and only verify it."""
    return mandate_service.create_intent_mandate(
        principal_id=req.principal_id, agent_id=req.agent_id,
        instructions=req.instructions, max_amount=req.max_amount,
    )


@app.post("/v1/mandates/cart", response_model=CartMandate, tags=["payments"])
def create_cart(req: CartRequest) -> CartMandate:
    return mandate_service.create_cart_mandate(
        principal_id=req.principal_id, intent_mandate=req.intent_mandate,
        sku=req.sku, quote_id=req.quote_id, amount=req.amount,
        trust_token_ref=req.trust_token_ref, items=req.items, bundle_id=req.bundle_id,
    )


@app.post("/v1/pay", response_model=OrderResult, tags=["payments"])
def pay(req: PayRequest) -> OrderResult:
    """Settle -- but only after re-verifying every gate. Returns 200 with
    status=REJECTED rather than an HTTP error: a refused purchase is a
    successful, well-formed answer to the question the agent asked, and it
    carries a reason code the agent can act on."""
    return orchestrator.execute(
        principal_id=req.principal_id, intent=req.intent_mandate, cart=req.cart_mandate
    )


@app.get("/v1/orders", tags=["payments"])
def orders(limit: int = 25) -> dict:
    return {"orders": db.query(
        """SELECT order_id, sku, amount::float AS amount, currency, rail, settlement_ref,
                  status, reason_code, agent_id,
                  extract(epoch FROM created_at) AS created_at
             FROM orders ORDER BY created_at DESC LIMIT %s""",
        (limit,),
    )}


@app.get("/v1/orders/{order_id}", tags=["payments"])
def order_detail(order_id: str) -> dict:
    row = db.query_one("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    if not row:
        raise HTTPException(status_code=404, detail="order not found")
    return row
