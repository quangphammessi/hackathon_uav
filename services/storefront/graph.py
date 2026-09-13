"""Multi-agent orchestration with LangGraph (proposal §5.2, Figure 3).

    planner -> retriever -> spec_extraction -> schema_validator
        --pass--> pricing_agent -> trust_agent --pass--> response_composer
        --fail--> repair -> retriever (bounded retries)
                                            --fail--> reject

The split-service version of this graph differs from the monolith in one
important way: `pricing_agent` and `trust_agent` are now *network* calls to
the pricing and verification services. That is deliberate and it is where the
architecture earns its keep -- those two services own their own data and can
be scaled, deployed and rate-limited independently of the storefront. It also
means those nodes can fail in ways a function call cannot, so both are
wrapped to turn a transport failure into a clean, machine-readable rejection
rather than a 500 propagating to the buyer agent.

The invariant from §5.4 holds unchanged: every numeric and factual field in
the final Offer is copied verbatim from the pricing engine, the product store
and the verification service. The LLM touches only query understanding
(planner/repair). It cannot invent a price, a spec or a trust status, because
it is never asked to produce one.
"""
from __future__ import annotations

import logging
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from agentmarket_core import llm
from agentmarket_core.clients import PricingClient, ServiceError, VerificationClient
from agentmarket_core.config import (
    TOPIC_STOREFRONT_OFFER,
    TOPIC_STOREFRONT_REJECTION,
    settings,
)
from agentmarket_core.models import Offer, PriceQuote, RejectedOffer, VerificationResult
from agentmarket_core.tracing import Tracer, new_trace_id

log = logging.getLogger("agentmarket.storefront.graph")

# The agent's stated ceiling applies to the *final* price, which the pricing
# engine usually sets below list. Filtering candidates strictly on list price
# would discard products that end up inside the budget once quoted, so the
# retrieval filter carries slack and the real check happens after pricing.
MAX_PRICE_SLACK = 1.25


class GraphState(TypedDict, total=False):
    trace_id: str
    agent_id: str
    query: str
    plan: dict
    excluded_skus: list[str]
    candidate_sku: Optional[str]
    validation_error: Optional[str]
    retries: int
    quote: Optional[dict]
    verification: Optional[dict]
    result: dict
    outcome: str  # "OFFER" | "REJECTED"


def matches_constraints(commercial: dict, spec, plan: dict) -> tuple[bool, Optional[str]]:
    max_price = plan.get("max_price")
    if max_price is not None and commercial["list_price"] > max_price * MAX_PRICE_SLACK:
        return False, f"list_price {commercial['list_price']} too far above max_price {max_price}"

    # Required terms are a soft signal, not an AND-gate. An LLM (or the
    # keyword fallback) can propose terms that are directionally right but not
    # literal substrings of the catalog text -- a synonym, or a unit phrase
    # like "under 500g". Hard-failing on every missing term rejects perfectly
    # good candidates; we only fail when NONE of the terms appear, which means
    # the match is not merely imperfect but plausibly the wrong product.
    required_terms = (plan.get("required_terms") or [])[:3]
    if required_terms:
        haystack = f"{spec.name} {spec.description} {spec.attributes}".lower()
        if not any(t.lower() in haystack for t in required_terms):
            return False, f"none of the required terms {required_terms} found on candidate"
    return True, None


class StorefrontGraph:
    def __init__(self, product_store, vector_store, tracer: Tracer,
                 pricing: PricingClient, verification: VerificationClient, bus=None) -> None:
        self.products = product_store
        self.vectors = vector_store
        self.tracer = tracer
        self.pricing = pricing
        self.verification = verification
        self.bus = bus
        self._graph = self._build()

    # ---------------------------------------------------------------- nodes
    def _planner(self, state: GraphState) -> GraphState:
        with self.tracer.span(state["trace_id"], "planner", {"query": state["query"]}) as span:
            plan = llm.plan_query(state["query"])
            span["outputs"] = {"plan": plan}
        return {**state, "plan": plan, "excluded_skus": [], "retries": 0}

    def _retriever(self, state: GraphState) -> GraphState:
        with self.tracer.span(state["trace_id"], "retriever", {"plan": state["plan"]}) as span:
            excluded = set(state.get("excluded_skus", []))
            plan = state["plan"]
            max_price = plan.get("max_price")
            hits = self.vectors.search(
                plan["search_query"],
                top_k=8,
                max_price=max_price * MAX_PRICE_SLACK if max_price else None,
            )
            candidate = next((sku for sku, _ in hits if sku not in excluded), None)
            span["outputs"] = {"hits": [{"sku": s, "score": round(sc, 4)} for s, sc in hits],
                               "chosen": candidate}
        return {**state, "candidate_sku": candidate}

    def _spec_extraction(self, state: GraphState) -> GraphState:
        with self.tracer.span(state["trace_id"], "spec_extraction",
                              {"sku": state.get("candidate_sku")}) as span:
            sku = state.get("candidate_sku")
            spec = self.products.get(sku) if sku else None
            span["outputs"] = {"found": spec is not None,
                               "name": spec.name if spec else None}
        return state

    def _schema_validator(self, state: GraphState) -> GraphState:
        with self.tracer.span(state["trace_id"], "schema_validator") as span:
            sku = state.get("candidate_sku")
            if not sku:
                span["outputs"] = {"ok": False, "error": "no candidate"}
                return {**state, "validation_error": "NO_CANDIDATES"}

            spec = self.products.get(sku)
            commercial = self.products.get_commercial(sku)
            if not spec or not commercial:
                span["outputs"] = {"ok": False, "error": "unknown sku"}
                return {**state, "validation_error": "NO_CANDIDATES"}

            ok, error = matches_constraints(commercial, spec, state["plan"])
            span["outputs"] = {"ok": ok, "error": error}
            return {**state, "validation_error": None if ok else error}

    def _repair(self, state: GraphState) -> GraphState:
        with self.tracer.span(state["trace_id"], "repair",
                              {"error": state.get("validation_error")}) as span:
            retries = state.get("retries", 0) + 1
            excluded = list(
                (set(state.get("excluded_skus", [])) | {state.get("candidate_sku")}) - {None}
            )
            new_query = llm.repair_query(
                state["plan"]["search_query"], state.get("validation_error") or ""
            )
            plan = {**state["plan"], "search_query": new_query}
            span["outputs"] = {"retries": retries, "new_query": new_query, "excluded": excluded}
        return {**state, "plan": plan, "excluded_skus": excluded, "retries": retries}

    def _pricing_agent(self, state: GraphState) -> GraphState:
        with self.tracer.span(state["trace_id"], "pricing_agent",
                              {"sku": state["candidate_sku"]}) as span:
            try:
                quote = self.pricing.quote(state["candidate_sku"])
            except ServiceError as exc:
                span["outputs"] = {"error": exc.detail}
                # A pricing outage must not produce a guessed price. No quote,
                # no offer -- the agent gets an honest, actionable reason code.
                return {**state, "validation_error": "PRICING_UNAVAILABLE", "quote": None}
            span["outputs"] = quote.model_dump()
        return {**state, "quote": quote.model_dump()}

    def _trust_agent(self, state: GraphState) -> GraphState:
        with self.tracer.span(state["trace_id"], "trust_agent",
                              {"sku": state["candidate_sku"]}) as span:
            sku = state["candidate_sku"]
            try:
                token_id, reason = self.verification.ensure_token(sku)
                if token_id is None:
                    result = VerificationResult(
                        trust_token_ref=sku, status="FAIL", confidence=0.0, reason_code=reason,
                    )
                else:
                    result = self.verification.verify(token_id)
            except ServiceError as exc:
                # Fail closed. An unverifiable product is not offered, ever --
                # the gate exists precisely so that "we couldn't check" and
                # "it's fine" are different answers.
                result = VerificationResult(
                    trust_token_ref=sku, status="FAIL", confidence=0.0,
                    reason_code="VERIFICATION_UNAVAILABLE",
                )
                span["outputs"] = {"error": exc.detail}
            span["outputs"] = {**span.get("outputs", {}), **result.model_dump()}
        return {**state, "verification": result.model_dump()}

    def _response_composer(self, state: GraphState) -> GraphState:
        with self.tracer.span(state["trace_id"], "response_composer") as span:
            sku = state["candidate_sku"]
            spec = self.products.get(sku)
            offer = Offer(
                sku=sku,
                name=spec.name,
                attributes=spec.attributes,
                price=PriceQuote(**state["quote"]),
                trust_token_ref=state["verification"]["trust_token_ref"],
                trust_status=state["verification"]["status"],
                trust_confidence=state["verification"]["confidence"],
            )
            span["outputs"] = {"offer_id": offer.offer_id, "amount": offer.price.amount,
                               "outcome": "OFFER", "query": state["query"]}
        if self.bus:
            self.bus.publish(TOPIC_STOREFRONT_OFFER, {
                "sku": sku, "offer_id": offer.offer_id, "amount": offer.price.amount,
                "trace_id": state["trace_id"], "agent_id": state.get("agent_id"),
            })
        return {**state, "result": offer.model_dump(), "outcome": "OFFER"}

    def _reject(self, state: GraphState) -> GraphState:
        with self.tracer.span(state["trace_id"], "reject") as span:
            verification = state.get("verification") or {}
            reason = (verification.get("reason_code")
                      or state.get("validation_error")
                      or "NO_MATCHING_PRODUCT")
            rejected = RejectedOffer(
                sku=state.get("candidate_sku"),
                query=state["query"],
                reason_code=reason,
                detail=(f"storefront could not produce a verifiable offer after "
                        f"{state.get('retries', 0)} repair attempt(s)"),
            )
            span["outputs"] = {**rejected.model_dump(), "outcome": "REJECTED",
                               "query": state["query"]}
        if self.bus:
            self.bus.publish(TOPIC_STOREFRONT_REJECTION, {
                "sku": state.get("candidate_sku"), "reason_code": reason,
                "trace_id": state["trace_id"], "agent_id": state.get("agent_id"),
            })
        return {**state, "result": rejected.model_dump(), "outcome": "REJECTED"}

    # ------------------------------------------------------------ routing
    def _route_after_validation(self, state: GraphState) -> str:
        if state.get("validation_error") is None:
            return "pricing_agent"
        if state.get("retries", 0) >= settings.max_repair_retries:
            return "reject"
        return "repair"

    def _route_after_pricing(self, state: GraphState) -> str:
        return "reject" if state.get("quote") is None else "trust_agent"

    def _route_after_trust(self, state: GraphState) -> str:
        verification = state.get("verification") or {}
        return "response_composer" if verification.get("status") == "PASS" else "reject"

    # --------------------------------------------------------------- build
    def _build(self):
        g = StateGraph(GraphState)
        for name, fn in [
            ("planner", self._planner), ("retriever", self._retriever),
            ("spec_extraction", self._spec_extraction), ("schema_validator", self._schema_validator),
            ("repair", self._repair), ("pricing_agent", self._pricing_agent),
            ("trust_agent", self._trust_agent), ("response_composer", self._response_composer),
            ("reject", self._reject),
        ]:
            g.add_node(name, fn)

        g.set_entry_point("planner")
        g.add_edge("planner", "retriever")
        g.add_edge("retriever", "spec_extraction")
        g.add_edge("spec_extraction", "schema_validator")
        g.add_conditional_edges("schema_validator", self._route_after_validation, {
            "pricing_agent": "pricing_agent", "repair": "repair", "reject": "reject",
        })
        g.add_edge("repair", "retriever")
        g.add_conditional_edges("pricing_agent", self._route_after_pricing, {
            "trust_agent": "trust_agent", "reject": "reject",
        })
        g.add_conditional_edges("trust_agent", self._route_after_trust, {
            "response_composer": "response_composer", "reject": "reject",
        })
        g.add_edge("response_composer", END)
        g.add_edge("reject", END)
        return g.compile()

    # ----------------------------------------------------------------- run
    def run(self, agent_id: str, query: str) -> dict:
        trace_id = new_trace_id()
        final_state = self._graph.invoke(
            {"trace_id": trace_id, "agent_id": agent_id, "query": query}
        )
        return {
            "trace_id": trace_id,
            "outcome": final_state["outcome"],
            "result": final_state["result"],
        }
