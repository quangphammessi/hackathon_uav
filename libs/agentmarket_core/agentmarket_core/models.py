"""Shared, strictly-validated data contracts.

Proposal §3, design principle 3: "Deterministic contracts at every boundary
... validated against a versioned JSON Schema before it crosses a service
boundary." These pydantic models (extra="forbid", so an unknown field is a
hard validation error, not a silent pass-through) ARE that schema for the
MVP -- every inter-subsystem call in this codebase passes one of these
objects, never a bare dict.
"""
from __future__ import annotations

import time
import uuid
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------------------
# Subsystem 2 -- Agentic RAG Storefront (proposal §5)
# ---------------------------------------------------------------------------

class ProductSpec(Strict):
    """Canonical, typed product record -- the structured product graph's
    source of truth (proposal §5.1). The LLM never invents these fields;
    they are copied verbatim from data/products.json."""

    sku: str
    gtin: str
    name: str
    category: str
    description: str
    attributes: dict
    currency: str = "AUD"


class PriceQuote(Strict):
    """Returned by the pricing engine's Quote API (proposal §4.4)."""

    quote_id: str = Field(default_factory=lambda: _id("q"))
    sku: str
    amount: float
    currency: str = "AUD"
    spread: float
    fair_value: float
    issued_at: float = Field(default_factory=time.time)
    valid_until: float
    guardrails_applied: list[str] = Field(default_factory=list)


class VerificationResult(Strict):
    """Returned by the Verification API (proposal §6.3)."""

    trust_token_ref: str
    status: Literal["PASS", "FAIL"]
    confidence: float
    reason_code: Optional[str] = None
    checked_at: float = Field(default_factory=time.time)


class Offer(Strict):
    """The deterministic JSON contract handed to the buyer agent
    (proposal §5.3). schema_version lets a consumer pin to a known shape."""

    schema_version: str = "1.3"
    offer_id: str = Field(default_factory=lambda: _id("offer"))
    sku: str
    name: str
    attributes: dict
    price: PriceQuote
    trust_token_ref: str
    trust_status: Literal["PASS", "FAIL"]
    trust_confidence: float


class RejectedOffer(Strict):
    """What the storefront returns when the trust or schema gate fails --
    a machine-readable reason code, never a partial/best-effort guess."""

    schema_version: str = "1.3"
    sku: Optional[str] = None
    query: str
    reason_code: str
    detail: str


# ---------------------------------------------------------------------------
# Subsystem 3 -- Deterministic Verification Pipeline (proposal §6)
# ---------------------------------------------------------------------------

class ProvenanceEvent(Strict):
    sku: str
    batch: str
    event_type: str
    biz_step: str
    location: str
    actor: str
    ts: str
    note: Optional[str] = None


class TrustToken(Strict):
    """A W3C-Verifiable-Credential-shaped attestation (proposal §6.2).
    Signed by the platform's Ed25519 keypair, standing in for a DID-based
    signature over a real trust-token service."""

    token_id: str = Field(default_factory=lambda: _id("vc"))
    context: list[str] = Field(default_factory=lambda: ["https://www.w3.org/2018/credentials/v1"])
    type: list[str] = Field(default_factory=lambda: ["VerifiableCredential", "TrustToken"])
    issuer: str
    issuance_date: str
    gs1_digital_link: str
    credential_subject: dict
    event_chain_hash: str
    proof: dict
    revoked: bool = False


# ---------------------------------------------------------------------------
# Subsystem 4 -- Payments (proposal §6.4)
# ---------------------------------------------------------------------------

class IntentMandate(Strict):
    """AP2-style Intent Mandate -- the buyer principal's signed, upfront
    authorization for the agent to act (proposal §6.4)."""

    mandate_id: str = Field(default_factory=lambda: _id("intent"))
    agent_id: str
    principal_id: str
    instructions: str
    max_amount: float
    currency: str = "AUD"
    issued_at: float = Field(default_factory=time.time)
    signature: str


class CartMandate(Strict):
    """AP2-style Cart Mandate -- binds the signed authorization to the
    EXACT sku/price the storefront quoted. Non-repudiable: the payment
    orchestrator never has to trust the agent's word for what it's buying."""

    mandate_id: str = Field(default_factory=lambda: _id("cart"))
    intent_mandate_id: str
    agent_id: str
    sku: str
    quote_id: str
    amount: float
    currency: str = "AUD"
    trust_token_ref: str
    signed_at: float = Field(default_factory=time.time)
    signature: str


class OrderResult(Strict):
    order_id: str = Field(default_factory=lambda: _id("order"))
    sku: str
    amount: float
    currency: str = "AUD"
    rail: Optional[Literal["card_network", "stablecoin_x402"]] = None
    settlement_ref: str = ""
    status: Literal["SETTLED", "REJECTED"]
    reason_code: Optional[str] = None
    settled_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Cross-cutting: observability + market data (proposal §5.5, §4.1)
# ---------------------------------------------------------------------------

class TraceSpan(Strict):
    """One LangGraph node execution, shaped like an OpenTelemetry GenAI span
    (proposal §5.5). Persisted to Postgres and published on the bus so the Ops
    Dashboard can replay a run node-by-node."""

    trace_id: str
    span_id: str = Field(default_factory=lambda: _id("span"))
    name: str
    service: str
    started_at: float
    ended_at: float
    duration_ms: float
    attributes: dict = Field(default_factory=dict)
    status: Literal["OK", "ERROR"] = "OK"


class CompetitorObservation(Strict):
    """A single scraped/fed competitor price, before the outlier filter
    decides whether it is allowed to move our fair value (proposal §4.1)."""

    sku: str
    competitor: str
    price: float
    currency: str = "AUD"
    observed_at: float = Field(default_factory=time.time)
    accepted: bool = True
    reason: Optional[str] = None


class ServiceHealth(Strict):
    service: str
    status: Literal["ok", "degraded"]
    version: str = "1.0.0"
    backends: dict = Field(default_factory=dict)
    detail: Optional[str] = None
