"""Agent Gateway -- the only service agents can reach (proposal §7.1, §8.1).

Responsibilities:
  * issue and verify agent identity (JWT here; OAuth2 client-credentials +
    mTLS at an Envoy/Kong edge in production)
  * fan requests out to the internal services, which are not exposed
  * aggregate read models for the Ops Dashboard, so the UI talks to one
    origin instead of four
  * stream live events over SSE for the dashboard's activity feed

The read-model aggregation matters more than it looks. Without it the browser
would need CORS grants and network routes to every internal service, which
means the "internal" services are not internal at all.

Why SSE and not WebSocket: the event feed is strictly one-directional and SSE
reconnects on its own, needs no protocol upgrade, and survives proxies that
mangle long-lived upgrades. A WebSocket would be a bigger hammer for a
problem that is entirely server-push.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque

import jwt
from fastapi import Body, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from agentmarket_core import db
from agentmarket_core.adapters.bus import EventBus
from agentmarket_core.clients import (
    PaymentsClient,
    PricingClient,
    ServiceError,
    StorefrontClient,
    VerificationClient,
)
from agentmarket_core.config import ALL_TOPICS, settings
from agentmarket_core.models import CartMandate, IntentMandate, OrderResult
from agentmarket_core.service import create_app
from agentmarket_core.tracing import Tracer

log = logging.getLogger("agentmarket.gateway")

storefront = StorefrontClient()
pricing = PricingClient()
verification = VerificationClient()
payments = PaymentsClient()

# Live event ring buffer + subscriber fan-out for SSE. Bounded on purpose: an
# unbounded buffer is a memory leak with a long uptime, and a dashboard that
# just connected does not need yesterday's events.
_recent_events: deque = deque(maxlen=200)
_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_name: str


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str


class IntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_id: str
    agent_id: str
    instructions: str
    max_amount: float


class PayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_id: str
    intent_mandate: IntentMandate
    sku: str
    quote_id: str
    amount: float
    trust_token_ref: str


def _fan_out(envelope: dict) -> None:
    """Bus handler -- runs on the consumer thread, so hand the event to the
    asyncio loop rather than touching the queues directly."""
    _recent_events.appendleft(envelope)
    if _loop is None:
        return
    for queue in list(_subscribers):
        try:
            _loop.call_soon_threadsafe(queue.put_nowait, envelope)
        except RuntimeError:
            pass


def _startup(bus: EventBus) -> None:
    global _loop
    _loop = asyncio.get_event_loop()
    for topic in ALL_TOPICS:
        bus.subscribe(topic, _fan_out)


app, ctx = create_app(
    "gateway",
    on_startup=_startup,
    health_probes=lambda: {
        "storefront": storefront.health(),
        "pricing": pricing.health(),
        "verification": verification.health(),
        "payments": payments.health(),
    },
)


# --- agent identity --------------------------------------------------------
def issue_agent_token(agent_name: str) -> dict:
    agent_id = f"agent_{uuid.uuid4().hex[:10]}"
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": agent_id, "name": agent_name, "iat": now,
            "exp": now + settings.jwt_ttl_seconds, "scope": "storefront:query payments:execute",
        },
        settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )
    return {"agent_id": agent_id, "token": token, "expires_in": settings.jwt_ttl_seconds}


def current_agent(authorization: str = Header(default="")) -> dict:
    """Auth dependency. Every agent-facing route depends on this; there is no
    unauthenticated path to the storefront or to payments."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    raw = authorization.split(" ", 1)[1]
    try:
        return jwt.decode(raw, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc


@app.post("/v1/agents/token", tags=["identity"])
def agent_token(req: TokenRequest) -> dict:
    return issue_agent_token(req.agent_name)


# --- agent-facing surface --------------------------------------------------
@app.post("/v1/query", tags=["agent"])
def query(req: QueryRequest, agent: dict = Depends(current_agent)) -> dict:
    try:
        return storefront.query(agent_id=agent["sub"], query=req.query)
    except ServiceError as exc:
        raise HTTPException(status_code=503, detail=f"storefront unavailable: {exc.detail}") from exc


@app.get("/v1/catalog", tags=["agent"])
def catalog() -> dict:
    try:
        return storefront._request("GET", "/v1/catalog").json()  # noqa: SLF001
    except ServiceError as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc


@app.post("/v1/principals/intent-mandate", response_model=IntentMandate, tags=["payments"])
def intent_mandate(req: IntentRequest) -> IntentMandate:
    try:
        return payments.create_intent_mandate(
            principal_id=req.principal_id, agent_id=req.agent_id,
            instructions=req.instructions, max_amount=req.max_amount,
        )
    except ServiceError as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc


@app.post("/v1/pay", response_model=OrderResult, tags=["payments"])
def pay(req: PayRequest, agent: dict = Depends(current_agent)) -> OrderResult:
    """Sign the cart mandate and settle in one call.

    The cart mandate is created here, from the gateway's own view of the
    offer, rather than accepting one the agent composed. That keeps the agent
    from signing a cart that disagrees with the quote it was given -- and the
    orchestrator re-checks it anyway.
    """
    try:
        cart = payments.create_cart_mandate(
            principal_id=req.principal_id, intent_mandate=req.intent_mandate,
            sku=req.sku, quote_id=req.quote_id, amount=req.amount,
            trust_token_ref=req.trust_token_ref,
        )
        return payments.pay(req.principal_id, req.intent_mandate, cart)
    except ServiceError as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc


# --- observability / ops read models --------------------------------------
@app.get("/v1/orders/{order_id}", tags=["payments"])
def order_detail(order_id: str) -> dict:
    """Full order record including the signed Intent and Cart Mandates.

    The gateway composes the cart mandate itself during `/v1/pay` and returns
    only the settlement result, so this is how a caller (or the demo console)
    inspects the authorization chain that actually authorized the payment.
    """
    try:
        return payments._request("GET", f"/v1/orders/{order_id}").json()  # noqa: SLF001
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code or 503, detail=exc.detail) from exc


@app.get("/v1/trace/{trace_id}", tags=["ops"])
def trace(trace_id: str) -> dict:
    spans = Tracer.spans(trace_id)
    if not spans:
        raise HTTPException(status_code=404, detail="unknown trace")
    return {"trace_id": trace_id, "spans": spans}


@app.get("/v1/traces", tags=["ops"])
def traces(limit: int = 25) -> dict:
    return {"traces": Tracer.recent_traces(limit=limit)}


@app.get("/v1/events", tags=["ops"])
def events(limit: int = 50) -> dict:
    return {"events": list(_recent_events)[:limit]}


@app.get("/v1/events/stream", tags=["ops"])
async def event_stream() -> StreamingResponse:
    """SSE feed of live bus events."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(queue)

    async def generator():
        try:
            yield f"data: {json.dumps({'topic': 'connection.open', 'ts': time.time()})}\n\n"
            while True:
                try:
                    envelope = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(envelope, default=str)}\n\n"
                except asyncio.TimeoutError:
                    # Comment frame: keeps idle proxies from closing the
                    # connection without polluting the client's event stream.
                    yield ": keepalive\n\n"
        finally:
            _subscribers.discard(queue)

    return StreamingResponse(generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.get("/v1/ops/overview", tags=["ops"])
def ops_overview() -> dict:
    """One call that populates the dashboard's header. Each section degrades
    independently: verification being down should not blank out pricing."""
    def safe(fn, default):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("ops overview section failed: %s", exc)
            return default

    return {
        "market": safe(lambda: pricing._request("GET", "/v1/market").json(), {"skus": []}),  # noqa: SLF001
        "provenance": safe(lambda: verification._request("GET", "/v1/provenance").json(), {"skus": []}),  # noqa: SLF001
        "ledger": safe(lambda: verification._request("GET", "/v1/ledger/integrity").json(), {}),  # noqa: SLF001
        "orders": safe(lambda: payments._request("GET", "/v1/orders?limit=10").json(), {"orders": []}),  # noqa: SLF001
        "traces": safe(lambda: {"traces": Tracer.recent_traces(limit=10)}, {"traces": []}),
        "services": {
            "storefront": storefront.health(), "pricing": pricing.health(),
            "verification": verification.health(), "payments": payments.health(),
        },
    }


@app.get("/v1/ops/market/{sku}", tags=["ops"])
def ops_market(sku: str) -> dict:
    try:
        return pricing._request("GET", f"/v1/market/{sku}").json()  # noqa: SLF001
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code or 503, detail=exc.detail) from exc


@app.get("/v1/ops/provenance/{sku}", tags=["ops"])
def ops_provenance(sku: str) -> dict:
    try:
        return verification._request("GET", f"/v1/provenance/{sku}").json()  # noqa: SLF001
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code or 503, detail=exc.detail) from exc


@app.get("/v1/ops/ledger", tags=["ops"])
def ops_ledger(limit: int = 50) -> dict:
    try:
        return verification._request("GET", f"/v1/ledger?limit={limit}").json()  # noqa: SLF001
    except ServiceError as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc
