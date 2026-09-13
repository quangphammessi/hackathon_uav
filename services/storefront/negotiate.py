"""The merchant's negotiating agent.

When the buyer's agent counters, this is what answers. It sits in the
storefront rather than in pricing because negotiating is not only a pricing
decision: the useful reply to "that is $40 over my budget" is often a
different product or a smaller kit, and only the storefront knows what the
buyer originally asked for and which candidates were eligible.

The division of labour is strict. The storefront decides *what to propose*;
the pricing service decides *what it may cost*, because only it holds cost and
MAP. So a concession is a question the storefront asks and cannot answer
itself, which is what keeps unit economics on one side of a network boundary
instead of relying on the storefront to be careful with numbers it was handed.

Four moves, tried in order of what the buyer most likely wants:

1. **Concede.** Meet the number if the floors allow it.
2. **Concede partially.** Move as far as the floors allow and say so.
3. **Propose an alternative.** A different eligible product that fits the
   number -- the brief's "propose a slightly older model that fits the
   constraint exactly".
4. **Restructure the kit.** Drop the least essential component so the rest
   fits, and report exactly what was removed.

Only when all four fail does it hold, and even then it returns its best
available price so the buyer's agent can decide rather than guess.
"""
from __future__ import annotations

import logging

from agentmarket_core.clients import PricingClient, ServiceError, VerificationClient
from agentmarket_core.config import TOPIC_NEGOTIATION_ROUND, settings
from agentmarket_core.domain import bundling, negotiation as neg
from agentmarket_core.models import (
    Bundle,
    CandidateAssessment,
    IntentPlan,
    NegotiationResult,
    PriceQuote,
)
from agentmarket_core.tracing import Tracer

log = logging.getLogger("agentmarket.storefront.negotiate")


class NegotiationError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class NegotiationAgent:
    def __init__(self, product_store, pricing: PricingClient,
                 verification: VerificationClient, tracer: Tracer,
                 store: neg.NegotiationStore, bus=None) -> None:
        self.products = product_store
        self.pricing = pricing
        self.verification = verification
        self.tracer = tracer
        self.store = store
        self.bus = bus

    # ------------------------------------------------------------------ API
    def counter(self, negotiation_id: str, target_amount: float, agent_id: str = "",
                reason: str = "") -> NegotiationResult:
        record = self.store.get(negotiation_id)
        if not record:
            raise NegotiationError("NEGOTIATION_NOT_FOUND", f"unknown negotiation {negotiation_id!r}")

        rounds = neg.rounds_from(record.get("rounds"))
        used = len([r for r in rounds if r.actor == "buyer_agent"])
        remaining = settings.max_negotiation_rounds - used
        current = float(record["current_amount"])
        context = record.get("context") or {}
        trace_id = context.get("trace_id") or negotiation_id

        if remaining <= 0:
            return self._finish(record, rounds, "EXHAUSTED", current,
                               neg.REASON_ROUNDS_EXHAUSTED,
                               f"negotiation limit of {settings.max_negotiation_rounds} rounds reached; "
                               f"the standing offer is ${current:,.2f}", None, None)

        # Turn number, not buyer-counter number. The transcript is read as a
        # conversation, and numbering the two speakers on different counters
        # produces "buyer 2, merchant 4" in a four-turn exchange.
        rounds.append(neg.buyer_round(len(rounds) + 1, target_amount, reason))

        with self.tracer.span(trace_id, "negotiate", {
            "negotiation_id": negotiation_id, "round": used + 1,
            "current": current, "target": round(target_amount, 2),
        }) as span:
            if context.get("bundle_skus"):
                result = self._counter_bundle(record, rounds, target_amount, context)
            else:
                result = self._counter_single(record, rounds, target_amount, context)
            span["outputs"] = {
                "outcome": result.outcome, "reason_code": result.reason_code,
                "amount": result.amount, "sku": result.sku,
                "rounds_remaining": result.rounds_remaining,
            }

        if self.bus:
            self.bus.publish(TOPIC_NEGOTIATION_ROUND, {
                "negotiation_id": negotiation_id, "agent_id": agent_id or record["agent_id"],
                "round": used + 1, "asked": round(target_amount, 2),
                "outcome": result.outcome, "amount": result.amount, "sku": result.sku,
            })
        return result

    # -------------------------------------------------------- single item
    def _counter_single(self, record: dict, rounds: list, target: float,
                        context: dict) -> NegotiationResult:
        sku = record["sku"]
        current = float(record["current_amount"])

        try:
            decision = self.pricing.concede(sku, current, target)
        except ServiceError as exc:
            raise NegotiationError("PRICING_UNAVAILABLE", exc.detail) from exc

        outcome = decision["outcome"]
        amount = float(decision["amount"])
        quote = self._quote_from(decision)

        if outcome == "CONCEDED":
            message = (f"agreed at ${amount:,.2f}" if amount < current
                       else f"the standing offer of ${amount:,.2f} already meets that")
            return self._finish(record, rounds, "CONCEDED", amount, None, message, quote, sku)

        # Price alone will not get there. Before holding, look for a product
        # that will -- the buyer asked for an outcome, not for this SKU.
        alternative = self._find_alternative(context, target, exclude={sku})
        if alternative:
            alt_sku, alt_quote, alt_name, alt_fit = alternative
            message = (f"cannot reach ${target:,.2f} on {self._name(sku)} without breaching a "
                       f"pricing floor; {alt_name} meets every stated requirement at "
                       f"${alt_quote.amount:,.2f}")
            return self._finish(record, rounds, "ALTERNATIVE_PROPOSED", alt_quote.amount,
                                None, message, alt_quote, alt_sku)

        if outcome == "PARTIAL_CONCESSION":
            message = (f"best available on {self._name(sku)} is ${amount:,.2f}; "
                       f"${target:,.2f} is below the floor this product may be sold at")
            return self._finish(record, rounds, "PARTIAL_CONCESSION", amount,
                                decision.get("reason_code"), message, quote, sku)

        message = (f"holding at ${current:,.2f}; no compliant configuration reaches "
                   f"${target:,.2f}")
        return self._finish(record, rounds, "HELD", current,
                            decision.get("reason_code") or neg.REASON_NO_ALTERNATIVE,
                            message, None, sku)

    # -------------------------------------------------------------- bundle
    def _counter_bundle(self, record: dict, rounds: list, target: float,
                        context: dict) -> NegotiationResult:
        skus: list[str] = list(context["bundle_skus"])
        current = float(record["current_amount"])
        plan = IntentPlan(**context["plan"])
        primary = record["sku"]

        if target >= current:
            message = f"the standing kit price of ${current:,.2f} already meets that"
            return self._finish(record, rounds, "CONCEDED", current, None, message, None, primary)

        # Component quotes are pre-discount; the buyer's number is post-
        # discount. Concessions are therefore negotiated in subtotal space and
        # the kit discount is reapplied at the end. Mixing the two is how a
        # kit that could have met the number gets needlessly restructured.
        subtotal = round(sum(self._item_amount(context, sku) for sku in skus), 2)
        keep_ratio = (current / subtotal) if subtotal else 1.0
        target_subtotal = target / keep_ratio if keep_ratio else target

        # Ask every component how far it can move, scaled to the size of the
        # gap. Proportional rather than flat because a $35 accessory cannot
        # absorb the same dollars as a $150 core item.
        ratio = max(0.0, min(1.0, target_subtotal / subtotal)) if subtotal else 1.0
        best_subtotal = 0.0
        per_item: dict[str, dict] = {}
        for sku in skus:
            amount = self._item_amount(context, sku)
            try:
                decision = self.pricing.concede(sku, amount, round(amount * ratio, 2))
            except ServiceError as exc:
                raise NegotiationError("PRICING_UNAVAILABLE", exc.detail) from exc
            per_item[sku] = decision
            best_subtotal += float(decision["amount"])
        best_total = round(best_subtotal * keep_ratio, 2)

        if best_total <= target:
            quotes = [q for q in (self._quote_from(d) for d in per_item.values()) if q]
            bundle = self._rebuild_bundle(context, plan, primary, quotes, target)
            message = (f"kit agreed at ${target:,.2f} across {len(skus)} items, "
                       f"each component still above its own floor")
            updated = {**context,
                       "bundle_quotes": [q.model_dump() for q in quotes]} if quotes else context
            return self._finish(record, rounds, "CONCEDED", target, None, message,
                                None, primary, bundle=bundle, context=updated)

        # Price alone is not enough: try removing the least essential item.
        restructured = self._restructure(context, plan, primary, skus, target)
        if restructured:
            bundle, removed = restructured
            # Both options, not just the one that fits. The buyer's agent is
            # optimising a budget it may be willing to stretch, and the choice
            # between "smaller kit inside your number" and "full kit slightly
            # over" is the buyer's to make, not the merchant's to make for them.
            message = (f"removed {removed} to reach ${bundle.total:,.2f}; the remaining "
                       f"{len(bundle.items)} items still cover every stated requirement. "
                       f"Keeping it is also possible at ${best_total:,.2f} for the full kit, "
                       f"which is ${best_total - target:,.2f} over the stated ceiling")
            updated = {
                **context,
                "bundle_skus": [item.sku for item in bundle.items],
                "bundle_quotes": [item.price.model_dump() for item in bundle.items],
            }
            return self._finish(record, rounds, "BUNDLE_RESTRUCTURED", bundle.total,
                                None, message, None, primary, bundle=bundle, context=updated)

        message = (f"best available for the full kit is ${best_total:,.2f}; below that, a "
                   f"component would have to be sold under its floor")
        return self._finish(record, rounds, "PARTIAL_CONCESSION", best_total,
                            neg.REASON_FLOOR_REACHED, message, None, primary)

    def _restructure(self, context: dict, plan: IntentPlan, primary: str,
                     skus: list[str], target: float) -> tuple[Bundle, str] | None:
        """Drop the least essential components until the kit fits the budget.

        Least essential is defined by the ranking that built the kit, so the
        first thing removed is the last thing that was added -- the item that
        earned its place by the narrowest margin. The core product is never
        dropped: without it the kit is no longer an answer to the request.
        """
        remaining = list(skus)
        removed: list[str] = []
        while len(remaining) > 2:
            victim = remaining[-1]
            if victim == primary:
                break
            remaining = remaining[:-1]
            removed.append(self._name(victim))
            try:
                priced = self.pricing.quote_bundle(remaining)
            except ServiceError:
                return None
            if priced["total"] <= target:
                quotes = [PriceQuote(**q) for q in priced["quotes"]]
                bundle = self._rebuild_bundle(context, plan, primary, quotes, priced["total"],
                                              discount=priced["bundle_discount"],
                                              subtotal=priced["subtotal"],
                                              guardrails=priced["guardrails_applied"])
                return bundle, " and ".join(removed)
        return None

    def _rebuild_bundle(self, context: dict, plan: IntentPlan, primary: str,
                        quotes: list[PriceQuote], total: float,
                        discount: float | None = None, subtotal: float | None = None,
                        guardrails: list[str] | None = None) -> Bundle | None:
        if len(quotes) < 2:
            return None
        specs = {q.sku: self.products.get(q.sku) for q in quotes}
        if any(spec is None for spec in specs.values()):
            return None
        computed_subtotal = subtotal if subtotal is not None else round(sum(q.amount for q in quotes), 2)
        computed_discount = discount if discount is not None else round(computed_subtotal - total, 2)
        trust = {q.sku: (context.get("trust", {}).get(q.sku, q.sku), "PASS") for q in quotes}
        return bundling.build_bundle(
            quotes, specs, plan, primary, trust, computed_subtotal,
            computed_discount, round(total, 2),
            list(guardrails or ["NEGOTIATED_CONCESSION"]), [],
        )

    # ------------------------------------------------------------- helpers
    def _find_alternative(self, context: dict, target: float, exclude: set[str]
                          ) -> tuple[str, PriceQuote, str, float] | None:
        """The best-fitting eligible candidate whose real quote fits the number.

        Candidates come from the assessment that produced the original offer,
        so an alternative has already passed every hard requirement the buyer
        stated. Cheaper is not the same as acceptable, and proposing something
        that fails a stated requirement is worse than holding firm.
        """
        candidates = [CandidateAssessment(**c) for c in (context.get("considered") or [])]
        for candidate in candidates:
            if candidate.sku in exclude or not candidate.eligible:
                continue
            try:
                quote = self.pricing.quote(candidate.sku)
            except ServiceError:
                continue
            if quote.amount > target:
                continue
            try:
                verified = self.verification.verify_skus([candidate.sku])
            except ServiceError:
                continue
            result = verified.get(candidate.sku) or {}
            if result.get("status") != "PASS":
                continue
            return candidate.sku, quote, candidate.name, candidate.fit_score
        return None

    def _item_amount(self, context: dict, sku: str) -> float:
        for quote in context.get("bundle_quotes") or []:
            if quote.get("sku") == sku:
                return float(quote["amount"])
        return 0.0

    def _name(self, sku: str) -> str:
        spec = self.products.get(sku)
        return spec.name if spec else sku

    @staticmethod
    def _quote_from(decision: dict) -> PriceQuote | None:
        quote = decision.get("quote")
        return PriceQuote(**quote) if quote else None

    def _finish(self, record: dict, rounds: list, outcome: str, amount: float,
                reason_code: str | None, message: str, quote: PriceQuote | None,
                sku: str | None, bundle: Bundle | None = None,
                context: dict | None = None) -> NegotiationResult:
        index = len(rounds) + 1
        rounds.append(neg.merchant_round(
            index, outcome, round(amount, 2), message, reason_code,
            quote.quote_id if quote else None, sku,
        ))
        used = len([r for r in rounds if r.actor == "buyer_agent"])
        self.store.append_round(
            record["negotiation_id"], rounds, outcome, amount,
            quote.quote_id if quote else record.get("current_quote"),
            sku if sku and sku != record["sku"] else None,
            context=context,
        )
        return NegotiationResult(
            negotiation_id=record["negotiation_id"],
            sku=sku or record["sku"],
            outcome=outcome,  # type: ignore[arg-type]
            reason_code=reason_code,
            amount=round(amount, 2),
            quote=quote,
            bundle=bundle,
            message=message,
            rounds_used=used,
            rounds_remaining=max(0, settings.max_negotiation_rounds - used),
            concession_from=round(float(record["opening_amount"]), 2),
            rounds=rounds,
        )
