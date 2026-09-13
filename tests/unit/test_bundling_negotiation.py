"""Dynamic bundling and agent-to-agent negotiation.

Both are pricing-adjacent, and both have the same failure mode if you get them
wrong: a discount that quietly breaches a floor. So most of what is asserted
here is what the system refuses to do.
"""
from __future__ import annotations

import pytest

from agentmarket_core.config import settings
from agentmarket_core.domain import bundling, intent
from agentmarket_core.domain import negotiation as neg
from agentmarket_core.models import CandidateAssessment, PriceQuote, ProductSpec


def spec(sku, name, product_type, category="gear", **attrs):
    return ProductSpec(sku=sku, gtin=sku, name=name, category=category,
                       description=f"{name} for testing.",
                       attributes={"product_type": product_type,
                                   "experience_level": "beginner",
                                   "ease_of_use": "simple", **attrs})


SHOE = spec("S1", "StepEasy Trail Shoe", "trail_shoe", "footwear",
            use_cases=["day_hike", "first_hike"])
MAT = spec("S2", "BaseCamp Insulated Mat", "sleeping_mat", use_cases=["camping", "cold_weather"])
BAG = spec("S3", "WarmNest Sleeping Bag", "sleeping_bag", use_cases=["camping", "first_hike"])
BAG2 = spec("S4", "OtherNest Sleeping Bag", "sleeping_bag", use_cases=["camping"])
PACK = spec("S5", "DayLite Daypack", "daypack", "bags", use_cases=["day_hike"])

PRICES = {"S1": 129.0, "S2": 109.0, "S3": 149.0, "S4": 139.0, "S5": 89.0}


def assessment(product, fit=0.5, eligible=True, price=None):
    return CandidateAssessment(
        sku=product.sku, name=product.name, similarity=fit, fit_score=fit,
        eligible=eligible, list_price=price if price is not None else PRICES[product.sku],
        disqualified_by=None if eligible else "failed a stated requirement",
    )


class TestBundleSelection:
    def test_a_kit_holds_one_of_each_kind_of_thing(self):
        """Two sleeping bags is not a kit."""
        plan = intent.decode("a complete beginner camping kit")
        chosen, _ = bundling.select_items(
            assessment(SHOE, 0.9), [(assessment(BAG, 0.8), BAG), (assessment(BAG2, 0.7), BAG2)],
            plan, SHOE)
        assert chosen == ["S1", "S3"]

    def test_the_budget_caps_the_kit_and_the_exclusion_is_recorded(self):
        plan = intent.decode("a complete beginner kit, under $250")
        chosen, dropped = bundling.select_items(
            assessment(SHOE, 0.9),
            [(assessment(MAT, 0.8), MAT), (assessment(BAG, 0.7), BAG)],
            plan, SHOE)
        assert "S3" not in chosen, "the third item would breach the budget"
        assert any(d["sku"] == "S3" for d in dropped)
        assert "budget" in next(d for d in dropped if d["sku"] == "S3")["reason"]

    def test_an_ineligible_item_never_enters_the_kit_and_says_why(self):
        """A kit that silently omits the ethical option is indistinguishable
        from a kit that never found one."""
        plan = intent.decode("a complete beginner kit")
        chosen, dropped = bundling.select_items(
            assessment(SHOE, 0.9), [(assessment(BAG, 0.8, eligible=False), BAG)], plan, SHOE)
        assert chosen == ["S1"]
        assert dropped and dropped[0]["reason"]

    def test_the_kit_never_exceeds_the_configured_item_cap(self):
        plan = intent.decode("absolutely everything for camping")
        candidates = [(assessment(p, 0.8), p) for p in (MAT, BAG, PACK)]
        chosen, _ = bundling.select_items(assessment(SHOE, 0.9), candidates, plan, SHOE,
                                          max_items=2)
        assert len(chosen) == 2

    def test_complements_from_the_product_graph_are_preferred(self):
        """Merchandising knowledge beats similarity: a bladder is not similar
        to a backpack, it belongs with one."""
        plan = intent.decode("a beginner kit")
        chosen, _ = bundling.select_items(
            assessment(SHOE, 0.9),
            [(assessment(PACK, 0.2), PACK), (assessment(MAT, 0.8), MAT)],
            plan, SHOE, complements=["S5"], max_items=2)
        assert chosen == ["S1", "S5"]

    def test_each_item_states_its_job_in_the_kit(self):
        plan = intent.decode("a kit for someone who gets cold easily")
        assert "cold" in bundling.role_in_bundle(MAT, plan, primary_sku="S1")
        assert bundling.role_in_bundle(SHOE, plan, primary_sku="S1").startswith("the core")


class TestBundlePricing:
    FLOORS = {"S1": 89.0, "S2": 74.9, "S3": 99.0}

    def quotes(self, sku):
        amounts = {"S1": 120.0, "S2": 100.0, "S3": 140.0}
        return PriceQuote(sku=sku, amount=amounts[sku], spread=0.03,
                          fair_value=amounts[sku] * 0.97, valid_until=9e9)

    def test_the_discount_comes_out_of_headroom_only(self):
        _, subtotal, discount, total, guardrails = bundling.price_bundle(
            ["S1", "S2", "S3"], self.quotes, self.FLOORS.get)
        assert subtotal == 360.0
        assert total == round(subtotal - discount, 2)
        assert discount <= subtotal * settings.bundle_max_discount_ratio + 0.01
        assert guardrails

    def test_a_bundle_can_never_push_a_component_below_its_own_floor(self):
        """A bundle must not become the route by which a product is sold
        under MAP."""
        tight = {"S1": 119.0, "S2": 99.5, "S3": 139.5}  # almost no headroom
        _, subtotal, discount, total, guardrails = bundling.price_bundle(
            ["S1", "S2", "S3"], self.quotes, tight.get)
        headroom = sum(self.quotes(s).amount - tight[s] for s in tight)
        assert discount <= headroom + 0.01
        assert "BUNDLE_FLOOR" in guardrails
        assert total >= sum(tight.values()) - 0.01

    def test_zero_headroom_means_zero_discount(self):
        at_floor = {"S1": 120.0, "S2": 100.0, "S3": 140.0}
        _, _, discount, total, _ = bundling.price_bundle(
            ["S1", "S2", "S3"], self.quotes, at_floor.get)
        assert discount == 0.0
        assert total == 360.0


class TestConcession:
    """`evaluate_concession` is the whole commercial policy, in one function."""

    FLOOR, FAIR, OPENING = 89.0, 116.0, 120.0

    def decide(self, target):
        return neg.evaluate_concession(self.OPENING, target, self.FLOOR, self.FAIR)

    def test_a_reachable_target_is_met_exactly(self):
        decision = self.decide(110.0)
        assert decision.outcome == "CONCEDED"
        assert decision.amount == 110.0

    def test_the_merchant_does_not_charge_more_than_the_buyer_offered(self):
        decision = self.decide(150.0)
        assert decision.outcome == "CONCEDED"
        assert decision.amount == self.OPENING

    def test_an_unreachable_target_moves_as_far_as_policy_allows(self):
        decision = self.decide(10.0)
        assert decision.outcome == "PARTIAL_CONCESSION"
        assert decision.amount < self.OPENING
        assert decision.amount >= self.FLOOR
        assert decision.reason_code in {neg.REASON_FLOOR_REACHED, neg.REASON_CONCESSION_LIMIT}

    def test_the_concession_is_bounded_even_when_the_floor_is_far_below(self):
        """Without a cap, the optimal strategy for every buyer's agent is to
        counter at $1, and an automated counterparty finds that out at once."""
        decision = neg.evaluate_concession(120.0, 1.0, floor=10.0, fair_value=116.0)
        limit = 120.0 - 116.0 * settings.max_concession_ratio
        assert decision.amount == pytest.approx(limit, abs=0.01)
        assert decision.reason_code == neg.REASON_CONCESSION_LIMIT

    def test_the_floor_binds_when_it_is_the_higher_of_the_two_bounds(self):
        decision = neg.evaluate_concession(120.0, 50.0, floor=110.0, fair_value=116.0)
        assert decision.amount == 110.0
        assert decision.reason_code == neg.REASON_FLOOR_REACHED

    def test_the_decision_never_carries_cost_or_map(self):
        """A counterparty is entitled to a price and a reason, not a margin."""
        payload = self.decide(10.0).to_dict()
        assert set(payload) == {"outcome", "amount", "reason_code", "moved_from"}
        assert self.FLOOR not in [v for k, v in payload.items() if k != "amount"]

    def test_item_floor_is_the_higher_of_margin_and_map(self):
        assert neg.item_floor(internal_cost=44.0, map_price=89.0) == 89.0
        assert neg.item_floor(internal_cost=100.0, map_price=50.0) == pytest.approx(
            100.0 * (1 + settings.min_margin_ratio), abs=0.01)


class TestNegotiationState:
    def test_rounds_accumulate_and_the_limit_is_enforceable(self):
        store = neg.NegotiationStore(persist=False)
        record = store.create("agent-1", "S1", 120.0, "q1", {"plan": {}})
        rounds = [neg.buyer_round(1, 100.0), neg.merchant_round(2, "CONCEDED", 100.0, "ok")]
        store.append_round(record["negotiation_id"], rounds, "CONCEDED", 100.0, "q2")

        reloaded = store.get(record["negotiation_id"])
        assert reloaded["current_amount"] == 100.0
        assert len([r for r in reloaded["rounds"] if r["actor"] == "buyer_agent"]) == 1

    def test_context_is_replaced_when_the_offer_changes_shape(self):
        """A restructured kit that leaves the old composition in context makes
        the next round negotiate against a bundle no longer on the table."""
        store = neg.NegotiationStore(persist=False)
        record = store.create("agent-1", "S1", 300.0, "q1",
                              {"bundle_skus": ["S1", "S2", "S3"]})
        store.append_round(record["negotiation_id"], [], "BUNDLE_RESTRUCTURED", 200.0, "q2",
                           context={"bundle_skus": ["S1", "S2"]})
        assert store.get(record["negotiation_id"])["context"]["bundle_skus"] == ["S1", "S2"]

    def test_rounds_survive_a_malformed_entry(self):
        assert neg.rounds_from([{"nonsense": True}, {"round": 1, "actor": "buyer_agent"}])
