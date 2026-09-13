"""Scoring candidates against a decoded intent, and proving values claims.

The two questions here are the ones a buyer's agent actually has: does this
product meet what I said, and is what the merchant says about it true?
"""
from __future__ import annotations

import pytest

from agentmarket_core.domain import claims as claims_mod
from agentmarket_core.domain import intent, matching
from agentmarket_core.models import ProductSpec

BEGINNER_SHOE = ProductSpec(
    sku="SKU-BEG", gtin="1", name="StepEasy Trail Shoe", category="footwear",
    description="A soft-collar trail shoe for first-time hikers.",
    attributes={
        "product_type": "trail_shoe", "experience_level": "beginner",
        "ease_of_use": "simple", "break_in_required": False, "wide_fit": True,
        "weight_g": 310, "waterproof_rating": "IPX4",
        "use_cases": ["day_hike", "first_hike"],
    },
    claims=["ethical_labour"],
)

EXPERT_BAG = ProductSpec(
    sku="SKU-EXP", gtin="2", name="GlacierDown -12C Sleeping Bag", category="gear",
    description="A 700-fill down bag rated to -12C.",
    attributes={
        "product_type": "sleeping_bag", "experience_level": "expert",
        "ease_of_use": "moderate", "temp_rating_c": -12, "weight_g": 1180,
        "use_cases": ["alpine", "cold_weather"],
    },
    claims=["animal_welfare"],
)

PRICING = {"list_price": 129.0, "inventory_units": 10}

ATTESTATION = [{
    "biz_step": "certification", "actor": "Fair Labor Association",
    "location": "Audit-VN-14", "ts": "2026-08-03T07:00:00Z",
    "note": "claim=ethical_labour; certificate=FLA-2026-0001; scope=batch",
}]


class TestHardConstraintsGate:
    def test_an_expert_product_is_disqualified_for_a_beginner(self):
        plan = intent.decode("a sleeping bag for someone who has never hiked before")
        result = matching.assess(EXPERT_BAG, PRICING, plan, [], similarity=0.9)
        assert not result.eligible
        assert "experience level" in result.disqualified_by

    def test_high_similarity_does_not_rescue_a_failed_hard_constraint(self):
        """This is the failure the brief describes: the semantically closest
        product is the wrong answer, and a score-only system ships it."""
        plan = intent.decode("a sleeping bag for a complete beginner")
        result = matching.assess(EXPERT_BAG, PRICING, plan, [], similarity=0.99)
        assert result.fit_score == 0.0
        assert not result.eligible

    def test_a_satisfied_candidate_is_eligible_and_scored(self):
        plan = intent.decode("trail shoes for someone who has never hiked before")
        result = matching.assess(BEGINNER_SHOE, PRICING, plan, [], similarity=0.5)
        assert result.eligible
        assert result.fit_score > 0
        assert result.disqualified_by is None


class TestSoftConstraintsRankWithoutGating:
    def test_a_missing_field_neither_passes_nor_fails(self):
        """A daypack has no temperature rating and must not be disqualified by
        a warmth preference that does not apply to it."""
        plan = intent.decode("something for a beginner who gets cold easily")
        result = matching.assess(BEGINNER_SHOE, PRICING, plan, [], similarity=0.4)
        assert result.eligible
        checked = {m.field for m in result.matched} | {m.field for m in result.unmet}
        assert "temp_rating_c" not in checked

    def test_unmet_preferences_are_reported_not_hidden(self):
        plan = intent.decode("beginner trail shoes for someone who gets cold easily")
        result = matching.assess(BEGINNER_SHOE, PRICING, plan, [], similarity=0.4)
        unmet = [m.requirement for m in result.unmet]
        assert any("cold" in r for r in unmet)
        assert result.eligible, "a preference must not disqualify"

    def test_weights_move_the_score(self):
        strong = intent.decode("beginner trail shoes for someone who gets cold easily")
        plain = intent.decode("beginner trail shoes")
        a = matching.assess(BEGINNER_SHOE, PRICING, strong, [], similarity=0.5)
        b = matching.assess(BEGINNER_SHOE, PRICING, plain, [], similarity=0.5)
        assert a.fit_score != b.fit_score


class TestEvidenceIsKept:
    def test_every_check_records_the_actual_value(self):
        plan = intent.decode("trail shoes for a beginner")
        result = matching.assess(BEGINNER_SHOE, PRICING, plan, [], similarity=0.5)
        match = next(m for m in result.matched if m.field == "experience_level")
        assert match.actual_value == "beginner"
        assert "beginner" in match.evidence

    def test_rejected_candidates_are_kept_for_the_explanation(self):
        """The rejected candidates and why they lost are what make the
        winner's case, so ranking must not filter them out."""
        plan = intent.decode("trail shoes for a beginner")
        ranked = matching.rank([
            matching.assess(EXPERT_BAG, PRICING, plan, [], 0.9),
            matching.assess(BEGINNER_SHOE, PRICING, plan, [], 0.2),
        ])
        assert len(ranked) == 2, "ineligible candidates must survive ranking"
        assert ranked[0].eligible, "the eligible candidate ranks first"
        assert not ranked[-1].eligible
        assert ranked[-1].disqualified_by


class TestValuesClaims:
    def test_an_attested_claim_verifies_with_its_evidence(self):
        results = claims_mod.verify_claims(["ethical_labour"], ATTESTATION, ["ethical_labour"])
        claim = results[0]
        assert claim.status == "VERIFIED"
        assert claim.attested_by == "Fair Labor Association"
        assert claim.certificate == "FLA-2026-0001"

    def test_an_asserted_claim_with_no_attestation_is_not_verified(self):
        """The greenwashing case: the merchant says so and nothing backs it."""
        results = claims_mod.verify_claims(["ethical_labour"], [], ["ethical_labour"])
        assert results[0].status == "ASSERTED_UNATTESTED"

    def test_an_unclaimed_value_is_reported_explicitly(self):
        results = claims_mod.verify_claims([], [], ["recycled_materials"])
        assert results[0].status == "NOT_CLAIMED"

    def test_a_certification_event_for_an_unknown_claim_is_ignored(self):
        """The vocabulary is closed. An open claim space cannot be audited."""
        rogue = [{**ATTESTATION[0], "note": "claim=cures_baldness; certificate=X"}]
        assert claims_mod.attestations_from_events(rogue) == {}

    def test_a_merchant_assertion_alone_never_satisfies_a_proof_demand(self):
        plan = intent.decode("trail shoes from a brand that can prove it is ethically made")
        unattested = claims_mod.verify_claims(BEGINNER_SHOE.claims, [], plan.values)
        result = matching.assess(BEGINNER_SHOE, PRICING, plan, unattested, similarity=0.9)
        assert not result.eligible
        assert "ethical" in (result.disqualified_by or "")

    def test_the_same_product_passes_once_the_chain_attests_it(self):
        plan = intent.decode("trail shoes from a brand that can prove it is ethically made")
        attested = claims_mod.verify_claims(BEGINNER_SHOE.claims, ATTESTATION, plan.values)
        result = matching.assess(BEGINNER_SHOE, PRICING, plan, attested, similarity=0.9)
        assert result.eligible
        evidence = next(m for m in result.matched if m.field == "claim").evidence
        assert "Fair Labor Association" in evidence

    def test_the_unattested_failure_explains_itself(self):
        plan = intent.decode("shoes from a brand that can prove it is ethically made")
        unattested = claims_mod.verify_claims(BEGINNER_SHOE.claims, [], plan.values)
        result = matching.assess(BEGINNER_SHOE, PRICING, plan, unattested, 0.5)
        miss = next(m for m in result.unmet if m.field == "claim")
        assert "no auditor" in miss.evidence or "nothing" in miss.evidence.lower()


class TestBudgetAtCandidateStage:
    def test_a_wildly_over_budget_product_is_dropped_early(self):
        plan = intent.decode("trail shoes under $50")
        result = matching.assess(BEGINNER_SHOE, {"list_price": 129.0}, plan, [], 0.8)
        assert not result.eligible

    def test_slack_keeps_products_that_will_price_inside_the_budget(self):
        """The quote usually lands below list, so filtering candidates hard on
        list price discards products the buyer could afford."""
        plan = intent.decode("trail shoes under $120")
        result = matching.assess(BEGINNER_SHOE, {"list_price": 129.0}, plan, [], 0.8)
        assert result.eligible
