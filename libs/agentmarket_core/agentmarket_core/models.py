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
    # Values the merchant asserts. Public, because marketing is public -- but
    # never evidence on its own: see domain/claims.py, which only counts a
    # claim once the provenance chain carries an auditor's attestation.
    claims: list[str] = Field(default_factory=list)
    role: str = "core"


# ---------------------------------------------------------------------------
# Intent decoding -- turning a human's delegated instruction into something
# the catalog can be evaluated against (challenge brief, "Decoding that intent
# accurately").
#
# The brief's example query is "beginner-friendly podcasting gear". Nothing in
# a product record says "beginner-friendly"; what the catalog holds is
# `experience_level`, `ease_of_use`, `break_in_required`. The gap between
# those two vocabularies is the entire problem, and an IntentPlan is the
# bridge: a decoded need, plus the machine-checkable predicates it implies,
# each one carrying the words it came from so the decode can be audited rather
# than trusted.
# ---------------------------------------------------------------------------

ConstraintOp = Literal["lte", "gte", "eq", "contains", "in", "not_eq"]


class IntentConstraint(Strict):
    """One machine-checkable predicate recovered from the query.

    `source_phrase` is what makes the decode reviewable: an evaluator (or a
    judge, or the buyer's agent) can see that `experience_level <= beginner`
    came from "he's never done it before" and disagree with it if it is wrong.
    A decode nobody can inspect is indistinguishable from a guess.
    """

    field: str
    op: ConstraintOp
    value: object
    kind: Literal["hard", "soft"] = "hard"
    source_phrase: str = ""
    weight: float = 1.0
    rationale: str = ""


class IntentPlan(Strict):
    """The decoded form of an agent's request."""

    schema_version: str = "2.0"
    raw_query: str
    search_query: str
    interpreted_need: str = ""
    use_cases: list[str] = Field(default_factory=list)
    experience_level: Optional[str] = None
    recipient: Optional[str] = None
    budget: Optional[float] = None
    budget_is_hard: bool = True
    values: list[str] = Field(default_factory=list)
    constraints: list[IntentConstraint] = Field(default_factory=list)
    bundle_intent: bool = False
    excluded_skus: list[str] = Field(default_factory=list)
    decoded_by: Literal["llm", "deterministic", "llm+rules"] = "deterministic"

    @property
    def hard_constraints(self) -> list[IntentConstraint]:
        return [c for c in self.constraints if c.kind == "hard"]


class ClaimVerification(Strict):
    """The answer to "is this values claim actually true?".

    VERIFIED means an independent auditor's certification event sits in the
    product's provenance chain and is covered by the signed credential.
    ASSERTED_UNATTESTED means the merchant says so and nothing backs it --
    which is reported, not silently dropped, because for a buyer who asked for
    ethical sourcing the difference between those two states is the whole
    question.
    """

    claim: str
    label: str
    status: Literal["VERIFIED", "ASSERTED_UNATTESTED", "NOT_CLAIMED"]
    attested_by: Optional[str] = None
    certificate: Optional[str] = None
    attested_at: Optional[str] = None
    evidence_event: Optional[dict] = None


class RequirementMatch(Strict):
    """One decoded requirement, checked against one product's real fields."""

    requirement: str
    source_phrase: str = ""
    satisfied: bool
    kind: Literal["hard", "soft"] = "hard"
    field: str = ""
    actual_value: object = None
    evidence: str = ""


class CandidateAssessment(Strict):
    """Why a candidate won or lost, in full. Returned for every candidate the
    retriever surfaced, not just the winner -- an agent comparing merchants
    can see the rejected ones and why, which is far more persuasive than a
    single unexplained recommendation."""

    sku: str
    name: str
    similarity: float
    fit_score: float
    eligible: bool
    disqualified_by: Optional[str] = None
    matched: list[RequirementMatch] = Field(default_factory=list)
    unmet: list[RequirementMatch] = Field(default_factory=list)
    claims: list[ClaimVerification] = Field(default_factory=list)
    list_price: Optional[float] = None


class GroundingReport(Strict):
    """Result of checking an LLM-written justification against the facts.

    The brief's stated failure mode for today's retailers is "hallucinated
    product claims". A merchant system that fixes that by asking a model
    nicely has not fixed it. Every number and every values word in the
    justification is checked against the verified fact sheet; on any violation
    the model's text is discarded and a deterministic template ships instead,
    and this report says which one the buyer got.
    """

    status: Literal["VERIFIED", "TEMPLATE_FALLBACK", "TEMPLATE_ONLY"]
    checked_numbers: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)
    composed_by: Literal["llm", "template"] = "template"


class OfferRationale(Strict):
    """The "why", which the brief asks for explicitly: not a list of SKUs but
    a logical justification of why this product matches this intention."""

    interpreted_need: str
    summary: str
    matched: list[RequirementMatch] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    verified_claims: list[ClaimVerification] = Field(default_factory=list)
    rejected_alternatives: list[dict] = Field(default_factory=list)
    grounding: GroundingReport


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


class BundleItem(Strict):
    """One component of a bundle, with the job it does in the kit.

    `role_in_bundle` is the part a flat cart cannot express: "this is the
    thing that keeps him warm" is why the item is in the proposal, and it is
    what lets the negotiation engine decide what may be dropped when the
    buyer's budget moves."""

    sku: str
    name: str
    role: Literal["core", "accessory"] = "accessory"
    role_in_bundle: str = ""
    price: PriceQuote
    trust_token_ref: str
    trust_status: Literal["PASS", "FAIL"]
    attributes: dict = Field(default_factory=dict)
    essential: bool = False


class Bundle(Strict):
    """A dynamically composed kit (challenge brief, "dynamic product
    bundling"). Priced as a unit: the discount comes out of the headroom
    above every component's own floor, never out of the floors themselves."""

    bundle_id: str = Field(default_factory=lambda: _id("bundle"))
    items: list[BundleItem]
    subtotal: float
    bundle_discount: float
    total: float
    currency: str = "AUD"
    guardrails_applied: list[str] = Field(default_factory=list)
    dropped: list[dict] = Field(default_factory=list)


class Offer(Strict):
    """The deterministic JSON contract handed to the buyer agent
    (proposal §5.3). schema_version lets a consumer pin to a known shape.

    v2 adds `rationale` and optional `bundle`. Everything factual in here is
    still copied verbatim from the pricing engine, the product store and the
    verification service; the rationale's prose is the only model-written
    field in the document, and it ships only after passing the grounding
    check that `rationale.grounding` reports.
    """

    schema_version: str = "2.0"
    offer_id: str = Field(default_factory=lambda: _id("offer"))
    sku: str
    name: str
    attributes: dict
    price: PriceQuote
    trust_token_ref: str
    trust_status: Literal["PASS", "FAIL"]
    trust_confidence: float
    rationale: Optional[OfferRationale] = None
    bundle: Optional[Bundle] = None
    negotiable: bool = True
    # Handed out with the offer so the buyer's agent can counter without
    # re-stating anything. The merchant keeps the context -- decoded intent,
    # eligible alternatives, kit composition -- so a counter-offer is answered
    # against the original request rather than against a bare number.
    negotiation_id: Optional[str] = None
    intent: Optional[IntentPlan] = None


class RejectedOffer(Strict):
    """What the storefront returns when the trust or schema gate fails --
    a machine-readable reason code, never a partial/best-effort guess.

    v2 carries the decode and the near-misses too. "No" is more useful to a
    buyer's agent when it comes with what was understood and which candidates
    failed which requirement: that is the difference between an agent
    re-querying blindly and an agent relaxing the one constraint that was
    binding.
    """

    schema_version: str = "2.0"
    sku: Optional[str] = None
    query: str
    reason_code: str
    detail: str
    intent: Optional[IntentPlan] = None
    considered: list[CandidateAssessment] = Field(default_factory=list)
    unmet_requirements: list[RequirementMatch] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent-to-agent negotiation (challenge brief, "Dynamic B2A Negotiation
# Protocol"). The merchant's agent answers the buyer's agent directly: it may
# concede, propose a different product, restructure a bundle, or hold -- and
# in every branch it explains itself in machine-readable terms without ever
# disclosing cost or MAP.
# ---------------------------------------------------------------------------

NegotiationOutcome = Literal[
    "CONCEDED",             # met the buyer's number within guardrails
    "PARTIAL_CONCESSION",   # moved as far as the floor allows, short of the ask
    "ALTERNATIVE_PROPOSED", # a different SKU that fits the constraint
    "BUNDLE_RESTRUCTURED",  # dropped or swapped a component to hit the number
    "HELD",                 # cannot move; floor reached
    "EXHAUSTED",            # round limit hit
]


class NegotiationRound(Strict):
    round: int
    actor: Literal["buyer_agent", "merchant_agent"]
    proposed_amount: Optional[float] = None
    outcome: Optional[NegotiationOutcome] = None
    reason_code: Optional[str] = None
    message: str = ""
    quote_id: Optional[str] = None
    sku: Optional[str] = None
    at: float = Field(default_factory=time.time)


class NegotiationResult(Strict):
    negotiation_id: str
    sku: str
    outcome: NegotiationOutcome
    reason_code: Optional[str] = None
    amount: float
    currency: str = "AUD"
    quote: Optional[PriceQuote] = None
    offer: Optional[Offer] = None
    bundle: Optional[Bundle] = None
    message: str = ""
    rounds_used: int = 0
    rounds_remaining: int = 0
    concession_from: Optional[float] = None
    rounds: list[NegotiationRound] = Field(default_factory=list)


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


class CartItem(Strict):
    """One line of a multi-item cart, each with its own quote and credential.

    A kit is one purchase decision and several products, so it is also several
    quotes and several trust credentials. Keeping them as lines rather than
    collapsing to a total is what lets settlement re-validate each one -- and
    refuse the whole cart when any single component's credential has been
    revoked since the offer was made.
    """

    sku: str
    quote_id: str
    amount: float
    trust_token_ref: str


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
    # Present when the cart is a bundle. `sku`/`quote_id`/`amount` then
    # describe the headline item and the cart total, and `items` carries the
    # lines settlement actually validates.
    items: list[CartItem] = Field(default_factory=list)
    bundle_id: Optional[str] = None
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
