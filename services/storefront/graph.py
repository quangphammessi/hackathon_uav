"""Multi-agent orchestration with LangGraph (proposal §5.2, Figure 3).

    intent_decode -> retrieve -> assess
        --no eligible candidate--> repair -> retrieve   (bounded retries)
        --eligible--> price -> verify -> compose -> respond
                                  --fail--> reject

Each node is one agent with one job, and the split is not decoration: the
pricing and verification nodes are *network* calls to services that own their
own data and their own secrets. Verification holds the signing key, so the
agent-facing storefront has no process-level access to it; pricing holds cost
and MAP, so the storefront cannot leak unit economics even by accident,
because it never has them. Both nodes turn a transport failure into a
machine-readable rejection rather than a 500, since an autonomous buyer needs
to branch on the answer rather than parse a stack trace.

What changed for v2 is where the intelligence sits. The old graph planned a
keyword query, retrieved one candidate, and checked it against a couple of
loose filters. This one decodes the request into predicates, retrieves through
two independent legs, scores every candidate against every predicate while
keeping the evidence, resolves values claims against signed provenance, and
composes a justification that is checked against the facts before it ships.

The §5.4 invariant is unchanged and now carries more weight: every factual
field in the Offer is copied verbatim from the product store, the pricing
engine and the verification service. The model contributes query understanding
and, if it is available and its output survives the grounding check, prose. It
cannot invent a price, a spec, a trust status or a values claim, because it is
never asked to produce one and its sentences are checked against the ones that
were.
"""
from __future__ import annotations

import logging
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from agentmarket_core import llm
from agentmarket_core.clients import PricingClient, ServiceError, VerificationClient
from agentmarket_core.config import (
    TOPIC_INTENT_DECODED,
    TOPIC_STOREFRONT_OFFER,
    TOPIC_STOREFRONT_REJECTION,
    settings,
)
from agentmarket_core.domain import bundling, intent as intent_mod, matching, rationale, retrieval
from agentmarket_core.domain.negotiation import NegotiationStore
from agentmarket_core.models import (
    Bundle,
    CandidateAssessment,
    ClaimVerification,
    IntentPlan,
    Offer,
    PriceQuote,
    ProductSpec,
    RejectedOffer,
)
from agentmarket_core.tracing import Tracer, new_trace_id

log = logging.getLogger("agentmarket.storefront.graph")

REASON_NO_CANDIDATES = "NO_CANDIDATES"
REASON_NO_ELIGIBLE = "NO_ELIGIBLE_CANDIDATE"
REASON_OVER_BUDGET = "OVER_BUDGET"
REASON_PRICING_UNAVAILABLE = "PRICING_UNAVAILABLE"
REASON_VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"


class GraphState(TypedDict, total=False):
    trace_id: str
    agent_id: str
    query: str
    plan: dict
    candidates: list[dict]
    primary_sku: Optional[str]
    excluded_skus: list[str]
    retries: int
    validation_error: Optional[str]
    quote: Optional[dict]
    bundle: Optional[dict]
    verification: Optional[dict]
    result: dict
    outcome: str  # "OFFER" | "REJECTED"


class StorefrontGraph:
    def __init__(self, product_store, vector_store, tracer: Tracer,
                 pricing: PricingClient, verification: VerificationClient, bus=None,
                 negotiations: NegotiationStore | None = None) -> None:
        self.products = product_store
        self.vectors = vector_store
        self.tracer = tracer
        self.pricing = pricing
        self.verification = verification
        self.bus = bus
        self.negotiations = negotiations
        self._graph = self._build()

    # ---------------------------------------------------------------- nodes
    def _intent_decode(self, state: GraphState) -> GraphState:
        with self.tracer.span(state["trace_id"], "intent_decode",
                              {"query": state["query"]}) as span:
            # The model proposes; the rules dispose. decode() re-runs its
            # deterministic layer over the raw query regardless, so an absent
            # or confused model degrades coverage, never correctness.
            plan = intent_mod.decode(state["query"], llm_plan=llm.decode_intent(state["query"]))
            span["outputs"] = {
                "decoded_by": plan.decoded_by,
                "interpreted_need": plan.interpreted_need,
                "search_query": plan.search_query,
                "budget": plan.budget,
                "budget_is_hard": plan.budget_is_hard,
                "values": plan.values,
                "bundle_intent": plan.bundle_intent,
                "constraints": [
                    {"field": c.field, "op": c.op, "value": c.value, "kind": c.kind,
                     "from": c.source_phrase, "why": c.rationale}
                    for c in plan.constraints
                ],
            }
        if self.bus:
            self.bus.publish(TOPIC_INTENT_DECODED, {
                "trace_id": state["trace_id"], "agent_id": state.get("agent_id"),
                "interpreted_need": plan.interpreted_need,
                "hard_constraints": len(plan.hard_constraints),
                "values": plan.values, "bundle_intent": plan.bundle_intent,
            })
        return {**state, "plan": plan.model_dump(), "excluded_skus": [], "retries": 0}

    def _retrieve(self, state: GraphState) -> GraphState:
        plan = IntentPlan(**state["plan"])
        plan.excluded_skus = list(state.get("excluded_skus") or [])
        with self.tracer.span(state["trace_id"], "retrieve",
                              {"search_query": plan.search_query}) as span:
            hits = retrieval.hybrid_candidates(plan, self.products, self.vectors)
            span["outputs"] = {
                "count": len(hits),
                "by_source": {
                    source: [sku for sku, _, s in hits if s == source]
                    for source in ("semantic", "structured", "both")
                },
                "hits": [{"sku": sku, "similarity": round(score, 4), "source": source}
                         for sku, score, source in hits],
            }
        return {**state, "candidates": [{"sku": sku, "similarity": score, "source": source}
                                        for sku, score, source in hits]}

    def _assess(self, state: GraphState) -> GraphState:
        """Score every candidate against every decoded predicate.

        Values claims are resolved in a single batched call to the
        verification service rather than one call per candidate: the answer
        comes from signed provenance, so it cannot be computed here, and
        twelve sequential round trips would put the whole latency budget into
        a question that is the same shape for every SKU.
        """
        plan = IntentPlan(**state["plan"])
        hits = state.get("candidates") or []
        with self.tracer.span(state["trace_id"], "assess",
                              {"candidates": len(hits)}) as span:
            skus = [h["sku"] for h in hits]
            claims_by_sku: dict[str, list[ClaimVerification]] = {}
            if skus:
                try:
                    claims_by_sku = self.verification.claims_batch(skus, plan.values)
                except ServiceError as exc:
                    # Fail closed on values: an unverifiable claim is not a
                    # satisfied one. Without this, a verification outage would
                    # silently turn "prove it" into "take our word for it".
                    log.warning("claims batch failed (%s); values constraints will not pass", exc.detail)
                    span["outputs"] = {"claims_error": exc.detail}

            assessments: list[CandidateAssessment] = []
            for hit in hits:
                spec = self.products.get(hit["sku"])
                if not spec:
                    continue
                public_pricing = self.products.get_public_pricing(hit["sku"])
                assessments.append(matching.assess(
                    spec, public_pricing, plan,
                    claims_by_sku.get(hit["sku"], []), hit["similarity"],
                ))

            ranked = matching.rank(assessments)
            eligible = [a for a in ranked if a.eligible]
            span["outputs"] = {
                **(span.get("outputs") or {}),
                "eligible": len(eligible),
                "ranked": [
                    {"sku": a.sku, "name": a.name, "fit": a.fit_score, "eligible": a.eligible,
                     "disqualified_by": a.disqualified_by,
                     "matched": [m.requirement for m in a.matched],
                     "unmet": [m.requirement for m in a.unmet]}
                    for a in ranked[:8]
                ],
            }
            error = None if eligible else (
                REASON_NO_ELIGIBLE if ranked else REASON_NO_CANDIDATES)
        return {
            **state,
            "candidates": [a.model_dump() for a in ranked],
            "primary_sku": eligible[0].sku if eligible else None,
            "validation_error": error,
        }

    def _repair(self, state: GraphState) -> GraphState:
        """Widen the search after nothing eligible came back.

        Only the *search* is widened. The decoded predicates are left alone on
        purpose: quietly relaxing a stated requirement to produce a sale is
        how an agent learns that the merchant's answers cannot be trusted, and
        a machine buyer that learns that never comes back.
        """
        plan = IntentPlan(**state["plan"])
        with self.tracer.span(state["trace_id"], "repair",
                              {"error": state.get("validation_error")}) as span:
            retries = state.get("retries", 0) + 1
            excluded = list({*(state.get("excluded_skus") or []),
                             *[c["sku"] for c in (state.get("candidates") or [])
                               if not c.get("eligible")]})
            widened = llm.repair_query(plan.search_query, state.get("validation_error") or "")
            if widened == plan.search_query:
                # No model, or nothing better to say: fall back to the raw
                # request, which is broader than the distilled search text.
                widened = plan.raw_query
            plan.search_query = widened
            span["outputs"] = {"retries": retries, "search_query": widened,
                               "excluded": excluded}
        return {**state, "plan": plan.model_dump(), "retries": retries,
                "excluded_skus": excluded}

    def _price(self, state: GraphState) -> GraphState:
        """Quote the primary, and the whole kit when the request implied one."""
        plan = IntentPlan(**state["plan"])
        primary_sku = state["primary_sku"]
        assessments = [CandidateAssessment(**c) for c in state["candidates"]]
        primary = next(a for a in assessments if a.sku == primary_sku)

        with self.tracer.span(state["trace_id"], "price", {"sku": primary_sku}) as span:
            bundle_skus: list[str] = []
            dropped: list[dict] = []
            if plan.bundle_intent:
                primary_spec = self.products.get(primary_sku)
                pairs = [(a, self.products.get(a.sku)) for a in assessments if a.sku != primary_sku]
                pairs = [(a, s) for a, s in pairs if s is not None]
                bundle_skus, dropped = bundling.select_items(
                    primary, pairs, plan, primary_spec,
                    complements=self.products.complements_of(primary_sku),
                )

            try:
                if len(bundle_skus) > 1:
                    priced = self.pricing.quote_bundle(bundle_skus)
                    quotes = [PriceQuote(**q) for q in priced["quotes"]]
                    quote = next(q for q in quotes if q.sku == primary_sku)
                    bundle_payload = {
                        "skus": bundle_skus, "quotes": [q.model_dump() for q in quotes],
                        "subtotal": priced["subtotal"], "discount": priced["bundle_discount"],
                        "total": priced["total"], "guardrails": priced["guardrails_applied"],
                        "dropped": dropped,
                    }
                else:
                    quote = self.pricing.quote(primary_sku)
                    bundle_payload = None
            except ServiceError as exc:
                # A pricing outage must never produce a guessed price.
                span["outputs"] = {"error": exc.detail}
                return {**state, "validation_error": REASON_PRICING_UNAVAILABLE, "quote": None}

            payable = bundle_payload["total"] if bundle_payload else quote.amount
            concession = None
            if plan.budget and payable > plan.budget:
                # The buyer's agent is optimising against a budget and this
                # product satisfies every other requirement. Refusing over a
                # gap the floors could absorb is the behaviour the brief
                # describes as the merchant losing a sale it should win, so
                # the merchant's side opens with the discount rather than
                # waiting to be asked.
                quote, bundle_payload, concession = self._concede_to_budget(
                    plan, quote, bundle_payload, primary_sku)
                payable = bundle_payload["total"] if bundle_payload else quote.amount

            over_budget = bool(plan.budget and plan.budget_is_hard and payable > plan.budget)
            span["outputs"] = {
                "quote": quote.model_dump(),
                "bundle_total": bundle_payload["total"] if bundle_payload else None,
                "payable": payable, "budget": plan.budget, "over_budget": over_budget,
                "auto_concession": concession,
            }
            if over_budget:
                return {**state, "validation_error": REASON_OVER_BUDGET,
                        "quote": quote.model_dump(), "bundle": bundle_payload}

        return {**state, "quote": quote.model_dump(), "bundle": bundle_payload,
                "validation_error": None}

    def _concede_to_budget(self, plan: IntentPlan, quote: PriceQuote,
                           bundle_payload: dict | None, primary_sku: str
                           ) -> tuple[PriceQuote, dict | None, dict | None]:
        """Try to bring a compliant proposal inside the stated budget.

        Only pricing may decide how far a price can move, so this asks rather
        than computes. For a kit it first asks for a proportional concession
        and, failing that, removes the component that earned its place by the
        narrowest margin -- in that order, because a buyer would rather pay
        less for everything than pay the same for less.
        """
        budget = float(plan.budget)
        try:
            if not bundle_payload:
                decision = self.pricing.concede(primary_sku, quote.amount, budget)
                if decision.get("quote"):
                    return PriceQuote(**decision["quote"]), None, decision
                return quote, None, decision

            subtotal = float(bundle_payload["subtotal"])
            keep_ratio = (float(bundle_payload["total"]) / subtotal) if subtotal else 1.0
            ratio = max(0.0, min(1.0, (budget / keep_ratio) / subtotal)) if subtotal else 1.0
            conceded: list[PriceQuote] = []
            for raw in bundle_payload["quotes"]:
                item = PriceQuote(**raw)
                decision = self.pricing.concede(item.sku, item.amount,
                                                round(item.amount * ratio, 2))
                conceded.append(PriceQuote(**decision["quote"]) if decision.get("quote") else item)
            new_subtotal = round(sum(q.amount for q in conceded), 2)
            new_total = round(new_subtotal * keep_ratio, 2)
            payload = {
                **bundle_payload,
                "quotes": [q.model_dump() for q in conceded],
                "subtotal": new_subtotal,
                "discount": round(new_subtotal - new_total, 2),
                "total": new_total,
                "guardrails": list(dict.fromkeys(
                    [*bundle_payload.get("guardrails", []), "BUDGET_CONCESSION"])),
            }
            primary = next((q for q in conceded if q.sku == primary_sku), quote)

            dropped = list(payload.get("dropped") or [])
            skus = list(payload["skus"])
            while new_total > budget and len(skus) > 2:
                victim = skus[-1]
                if victim == primary_sku:
                    break
                skus = skus[:-1]
                spec = self.products.get(victim)
                dropped.append({"sku": victim, "name": spec.name if spec else victim,
                                "reason": f"removed to bring the kit inside the ${budget:,.0f} budget"})
                repriced = self.pricing.quote_bundle(skus)
                new_total = float(repriced["total"])
                payload = {
                    **payload, "skus": skus, "quotes": repriced["quotes"],
                    "subtotal": repriced["subtotal"], "discount": repriced["bundle_discount"],
                    "total": new_total, "dropped": dropped,
                    "guardrails": list(dict.fromkeys(
                        [*repriced["guardrails_applied"], "BUDGET_CONCESSION"])),
                }
                primary = next((PriceQuote(**q) for q in repriced["quotes"]
                                if q["sku"] == primary_sku), primary)
            payload["dropped"] = dropped
            return primary, payload, {"outcome": "BUDGET_CONCESSION", "amount": new_total}
        except ServiceError as exc:
            log.warning("budget concession failed: %s", exc.detail)
            return quote, bundle_payload, None

    def _verify(self, state: GraphState) -> GraphState:
        """Trust-gate every item that is about to be offered.

        For a bundle this means every component, not just the headline
        product. A kit is a single purchase decision for the buyer, so one
        unverifiable component contaminates the whole proposal -- and the
        honest move is to drop that component and say so, not to ship it
        inside a bigger number where nobody looks.
        """
        skus = [state["primary_sku"]]
        if state.get("bundle"):
            skus = list(state["bundle"]["skus"])

        with self.tracer.span(state["trace_id"], "verify", {"skus": skus}) as span:
            try:
                results = self.verification.verify_skus(skus)
            except ServiceError as exc:
                # Fail closed: "we could not check" and "it is fine" must be
                # different answers, and only one of them sells a product.
                span["outputs"] = {"error": exc.detail}
                return {**state, "verification": {
                    "primary": {"trust_token_ref": state["primary_sku"], "status": "FAIL",
                                "confidence": 0.0, "reason_code": REASON_VERIFICATION_UNAVAILABLE},
                    "by_sku": {},
                }}
            primary = results.get(state["primary_sku"], {
                "trust_token_ref": state["primary_sku"], "status": "FAIL",
                "confidence": 0.0, "reason_code": "TOKEN_NOT_FOUND"})
            span["outputs"] = {"primary": primary, "checked": len(results),
                               "failed": [s for s, r in results.items() if r["status"] != "PASS"]}
        return {**state, "verification": {"primary": primary, "by_sku": results}}

    def _compose(self, state: GraphState) -> GraphState:
        plan = IntentPlan(**state["plan"])
        assessments = [CandidateAssessment(**c) for c in state["candidates"]]
        primary = next(a for a in assessments if a.sku == state["primary_sku"])
        spec = self.products.get(state["primary_sku"])
        quote = PriceQuote(**state["quote"])
        verification = state["verification"]
        trust = verification["primary"]

        with self.tracer.span(state["trace_id"], "compose", {"sku": spec.sku}) as span:
            bundle = self._assemble_bundle(state, plan, verification) if state.get("bundle") else None

            why = rationale.compose(
                spec, quote, primary, plan, trust["status"],
                rejected=[a for a in assessments if not a.eligible][:3],
                bundle=bundle,
                composer=llm.compose_rationale,
            )
            offer = Offer(
                sku=spec.sku, name=spec.name, attributes=spec.attributes,
                price=quote,
                trust_token_ref=trust["trust_token_ref"],
                trust_status=trust["status"],
                trust_confidence=trust["confidence"],
                rationale=why, bundle=bundle, intent=plan,
                negotiation_id=self._open_negotiation(state, plan, assessments, quote, bundle),
            )
            span["outputs"] = {
                "offer_id": offer.offer_id, "amount": quote.amount,
                "payable": bundle.total if bundle else quote.amount,
                "grounding": why.grounding.status,
                "grounding_violations": why.grounding.violations,
                "outcome": "OFFER", "query": state["query"],
            }
        if self.bus:
            self.bus.publish(TOPIC_STOREFRONT_OFFER, {
                "sku": spec.sku, "offer_id": offer.offer_id,
                "amount": bundle.total if bundle else quote.amount,
                "bundle_items": len(bundle.items) if bundle else 1,
                "grounding": why.grounding.status,
                "trace_id": state["trace_id"], "agent_id": state.get("agent_id"),
            })
        return {**state, "result": offer.model_dump(), "outcome": "OFFER"}

    def _open_negotiation(self, state: GraphState, plan: IntentPlan,
                          assessments: list[CandidateAssessment], quote: PriceQuote,
                          bundle: Bundle | None) -> str | None:
        """Record the context a counter-offer will need, and return its id.

        Opening the negotiation at offer time rather than at the first counter
        is what lets the buyer's agent reply with a number and nothing else.
        The alternatives it might be offered later are the ones that already
        passed every hard requirement in this decode, so a counter never gets
        answered with a product that fails a constraint the buyer stated.
        """
        if not self.negotiations:
            return None
        payable = bundle.total if bundle else quote.amount
        context = {
            "trace_id": state["trace_id"],
            "plan": plan.model_dump(),
            "considered": [a.model_dump() for a in assessments if a.eligible][:6],
            "trust": {sku: result["trust_token_ref"]
                      for sku, result in (state["verification"].get("by_sku") or {}).items()},
        }
        if bundle:
            context["bundle_skus"] = [item.sku for item in bundle.items]
            context["bundle_quotes"] = [item.price.model_dump() for item in bundle.items]
        try:
            record = self.negotiations.create(
                agent_id=state.get("agent_id") or "unknown", sku=state["primary_sku"],
                amount=payable, quote_id=quote.quote_id, context=context,
            )
            return record["negotiation_id"]
        except Exception as exc:  # noqa: BLE001 -- an offer must not fail because negotiation state did
            log.warning("could not open negotiation for offer: %s", exc)
            return None

    def _assemble_bundle(self, state: GraphState, plan: IntentPlan, verification: dict) -> Bundle | None:
        payload = state["bundle"]
        by_sku = verification.get("by_sku") or {}
        quotes = [PriceQuote(**q) for q in payload["quotes"]]

        kept, dropped = [], list(payload.get("dropped") or [])
        for quote in quotes:
            result = by_sku.get(quote.sku)
            if result and result["status"] == "PASS":
                kept.append(quote)
            else:
                spec = self.products.get(quote.sku)
                dropped.append({
                    "sku": quote.sku, "name": spec.name if spec else quote.sku,
                    "reason": f"trust verification failed ({(result or {}).get('reason_code', 'UNKNOWN')})",
                })
        if len(kept) < 2:
            return None

        # Dropping a component changes the total, so the kit is re-priced
        # rather than having the removed item's share subtracted here -- the
        # discount has to be recomputed against the floors that remain.
        subtotal = round(sum(q.amount for q in kept), 2)
        ratio = (payload["discount"] / payload["subtotal"]) if payload["subtotal"] else 0.0
        discount = round(subtotal * ratio, 2)
        specs: dict[str, ProductSpec] = {q.sku: self.products.get(q.sku) for q in kept}
        trust_map = {q.sku: (by_sku[q.sku]["trust_token_ref"], by_sku[q.sku]["status"])
                     for q in kept if q.sku in by_sku}
        return bundling.build_bundle(
            kept, specs, plan, state["primary_sku"], trust_map,
            subtotal, discount, round(subtotal - discount, 2),
            list(payload.get("guardrails") or []), dropped,
        )

    def _reject(self, state: GraphState) -> GraphState:
        plan = IntentPlan(**state["plan"]) if state.get("plan") else None
        assessments = [CandidateAssessment(**c) for c in (state.get("candidates") or [])]
        verification = (state.get("verification") or {}).get("primary") or {}

        with self.tracer.span(state["trace_id"], "reject") as span:
            reason = (verification.get("reason_code")
                      or state.get("validation_error")
                      or REASON_NO_CANDIDATES)
            unmet = []
            if assessments:
                unmet = assessments[0].unmet
            detail = self._rejection_detail(reason, state, assessments, plan)
            rejected = RejectedOffer(
                sku=state.get("primary_sku"), query=state["query"],
                reason_code=reason, detail=detail, intent=plan,
                # The near-misses and what each one failed. A "no" that says
                # which constraint was binding lets the buyer's agent relax
                # that one and re-ask, instead of guessing or walking away.
                considered=assessments[:5], unmet_requirements=unmet,
            )
            span["outputs"] = {"reason_code": reason, "detail": detail,
                               "outcome": "REJECTED", "query": state["query"]}
        if self.bus:
            self.bus.publish(TOPIC_STOREFRONT_REJECTION, {
                "sku": state.get("primary_sku"), "reason_code": reason,
                "trace_id": state["trace_id"], "agent_id": state.get("agent_id"),
            })
        return {**state, "result": rejected.model_dump(), "outcome": "REJECTED"}

    @staticmethod
    def _rejection_detail(reason: str, state: GraphState,
                          assessments: list[CandidateAssessment], plan: IntentPlan | None) -> str:
        if reason == REASON_OVER_BUDGET and plan:
            payable = (state.get("bundle") or {}).get("total") or (state.get("quote") or {}).get("amount")
            return (f"the best compliant configuration prices at ${payable:,.2f}, above the "
                    f"stated ${plan.budget:,.0f} ceiling; counter-offer to negotiate")
        if reason == REASON_NO_ELIGIBLE and assessments:
            blockers = [a.disqualified_by for a in assessments[:3] if a.disqualified_by]
            return ("no catalog item satisfies every stated requirement; the closest candidates "
                    "failed on: " + "; ".join(dict.fromkeys(blockers)))
        if reason == REASON_NO_CANDIDATES:
            return "no catalog item matched the decoded intent, semantically or structurally"
        if reason == REASON_PRICING_UNAVAILABLE:
            return "the pricing service is unavailable; no price was guessed"
        if reason == REASON_VERIFICATION_UNAVAILABLE:
            return "the verification service is unavailable; an unverifiable product is not offered"

        # Trust failures. These are the interesting refusals -- the product
        # matched the request and was still not sold -- so the reason has to be
        # specific enough for the buyer's agent to act on rather than a generic
        # "could not produce an offer".
        name = next((a.name for a in assessments if a.sku == state.get("primary_sku")),
                    state.get("primary_sku") or "the matched product")
        if reason == "CHAIN_GAP":
            return (f"{name} matched the request, but its supply-chain record is missing a "
                    f"required step, so no trust credential could be issued for the batch in "
                    f"stock; it is not offered rather than offered unverified")
        if reason == "CREDENTIAL_REVOKED":
            return (f"{name}'s trust credential has been revoked, so the product is withdrawn "
                    f"from sale until a new credential is issued")
        if reason == "NO_PROVENANCE_RECORD":
            return f"{name} has no supply-chain record at all, so nothing about it can be attested"
        if reason in {"SIGNATURE_INVALID", "CHAIN_HASH_MISMATCH"}:
            return (f"{name}'s credential failed cryptographic verification "
                    f"({reason}); the product is withheld")
        return (f"storefront could not produce a verifiable offer after "
                f"{state.get('retries', 0)} repair attempt(s)")

    # ------------------------------------------------------------ routing
    def _route_after_assess(self, state: GraphState) -> str:
        if state.get("validation_error") is None:
            return "price"
        if state.get("retries", 0) >= settings.max_repair_retries:
            return "reject"
        return "repair"

    def _route_after_price(self, state: GraphState) -> str:
        return "reject" if state.get("validation_error") else "verify"

    def _route_after_verify(self, state: GraphState) -> str:
        primary = (state.get("verification") or {}).get("primary") or {}
        return "compose" if primary.get("status") == "PASS" else "reject"

    # --------------------------------------------------------------- build
    def _build(self):
        g = StateGraph(GraphState)
        for name, fn in [
            ("intent_decode", self._intent_decode), ("retrieve", self._retrieve),
            ("assess", self._assess), ("repair", self._repair), ("price", self._price),
            ("verify", self._verify), ("compose", self._compose), ("reject", self._reject),
        ]:
            g.add_node(name, fn)

        g.set_entry_point("intent_decode")
        g.add_edge("intent_decode", "retrieve")
        g.add_edge("retrieve", "assess")
        g.add_conditional_edges("assess", self._route_after_assess, {
            "price": "price", "repair": "repair", "reject": "reject",
        })
        g.add_edge("repair", "retrieve")
        g.add_conditional_edges("price", self._route_after_price, {
            "verify": "verify", "reject": "reject",
        })
        g.add_conditional_edges("verify", self._route_after_verify, {
            "compose": "compose", "reject": "reject",
        })
        g.add_edge("compose", END)
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
