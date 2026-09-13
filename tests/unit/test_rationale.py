"""The justification, and the guard that stops it being fiction.

The brief names "hallucinated product claims" as the failure mode of today's
product data. A merchant system that fixes that by instructing a model nicely
has not fixed it, so these tests do the only thing that proves the guard is
real: hand the composer a model that lies, in each of the ways a model lies,
and assert the lie does not reach the buyer.
"""
from __future__ import annotations

import pytest

from agentmarket_core.domain import claims as claims_mod
from agentmarket_core.domain import intent, matching, rationale
from agentmarket_core.models import PriceQuote, ProductSpec

SHOE = ProductSpec(
    sku="SKU-BEG", gtin="1", name="StepEasy Trail Shoe", category="footwear",
    description="A soft-collar trail shoe for first-time hikers.",
    attributes={
        "product_type": "trail_shoe", "experience_level": "beginner",
        "ease_of_use": "simple", "break_in_required": False, "weight_g": 310,
        "use_cases": ["day_hike", "first_hike"],
    },
    claims=["ethical_labour"],
)

ATTESTATION = [{
    "biz_step": "certification", "actor": "Fair Labor Association",
    "location": "Audit-VN-14", "ts": "2026-08-03T07:00:00Z",
    "note": "claim=ethical_labour; certificate=FLA-2026-0001; scope=batch",
}]


@pytest.fixture
def quote():
    return PriceQuote(sku="SKU-BEG", amount=117.79, spread=0.03, fair_value=114.36,
                      valid_until=9e9)


@pytest.fixture
def plan():
    return intent.decode("trail shoes for someone who has never hiked before, under $150")


@pytest.fixture
def assessment(plan):
    verified = claims_mod.verify_claims(SHOE.claims, ATTESTATION, plan.values)
    return matching.assess(SHOE, {"list_price": 129.0}, plan, verified, similarity=0.7)


def compose(plan, assessment, quote, composer=None):
    return rationale.compose(SHOE, quote, assessment, plan, "PASS", composer=composer)


class TestTemplatePath:
    def test_without_a_model_a_real_justification_still_ships(self):
        """The deterministic path has to be good on its own, or the guard
        below is a threat rather than a choice."""
        plan_ = intent.decode("trail shoes for someone who has never hiked before")
        verified = claims_mod.verify_claims(SHOE.claims, ATTESTATION, plan_.values)
        result = compose(plan_, matching.assess(SHOE, {"list_price": 129.0}, plan_, verified, 0.7),
                         PriceQuote(sku="SKU-BEG", amount=117.79, spread=0.03,
                                    fair_value=114.36, valid_until=9e9))
        assert result.grounding.status == "TEMPLATE_ONLY"
        assert SHOE.name in result.summary
        assert "beginner" in result.summary
        assert len(result.summary) > 120

    def test_the_template_cites_the_attesting_body(self, plan, assessment, quote):
        result = compose(plan, assessment, quote)
        assert "Fair Labor Association" in result.summary

    def test_tradeoffs_are_stated_not_hidden(self, quote):
        plan_ = intent.decode("trail shoes for a beginner who gets cold easily")
        verified = claims_mod.verify_claims(SHOE.claims, ATTESTATION, plan_.values)
        assessment_ = matching.assess(SHOE, {"list_price": 129.0}, plan_, verified, 0.7)
        result = compose(plan_, assessment_, quote)
        assert result.tradeoffs, "an unmet preference must be reported"


class TestGroundingGuard:
    def test_an_invented_number_is_rejected(self, plan, assessment, quote):
        lie = lambda sheet: "A superb shoe, and at just $49.99 it is a bargain."  # noqa: E731
        result = compose(plan, assessment, quote, composer=lie)
        assert result.grounding.status == "TEMPLATE_FALLBACK"
        assert any("49.99" in v for v in result.grounding.violations)
        assert "49.99" not in result.summary

    def test_an_unattested_values_claim_is_rejected(self, plan, assessment, quote):
        """The model may not upgrade a claim the provenance chain does not
        make. This is the greenwashing guard applied to our own output."""
        lie = lambda sheet: "This shoe is made from recycled materials throughout."  # noqa: E731
        result = compose(plan, assessment, quote, composer=lie)
        assert result.grounding.status == "TEMPLATE_FALLBACK"
        assert any("recycled" in v.lower() for v in result.grounding.violations)

    @pytest.mark.parametrize("text", [
        "This is the best hiking shoe available.",
        "A guaranteed fit for your needs.",
        "Our top-rated beginner shoe.",
    ])
    def test_unverifiable_superlatives_are_rejected(self, plan, assessment, quote, text):
        result = compose(plan, assessment, quote, composer=lambda sheet: text)
        assert result.grounding.status == "TEMPLATE_FALLBACK"
        assert any("superlative" in v for v in result.grounding.violations)

    def test_a_faithful_justification_is_allowed_through(self, plan, assessment, quote):
        honest = lambda sheet: (  # noqa: E731
            "This product is rated for beginners and needs no breaking in. "
            "Its ethical labour claim is attested by the Fair Labor Association. "
            "It is quoted at $117.79.")
        result = compose(plan, assessment, quote, composer=honest)
        assert result.grounding.status == "VERIFIED"
        assert result.grounding.composed_by == "llm"
        assert result.summary == honest(None)

    def test_a_rounded_restatement_of_a_real_price_is_allowed(self, plan, assessment, quote):
        """$117.79 written as "about $118" is a paraphrase, not a fabrication."""
        result = compose(plan, assessment, quote,
                         composer=lambda sheet: "Quoted at about $118 for a beginner-rated shoe.")
        assert result.grounding.status == "VERIFIED"

    def test_a_composer_that_raises_does_not_break_the_offer(self, plan, assessment, quote):
        def boom(sheet):
            raise RuntimeError("model server exploded")

        result = compose(plan, assessment, quote, composer=boom)
        assert result.grounding.status == "TEMPLATE_ONLY"
        assert result.summary

    def test_an_empty_model_response_falls_back_silently(self, plan, assessment, quote):
        result = compose(plan, assessment, quote, composer=lambda sheet: "   ")
        assert result.grounding.status == "TEMPLATE_ONLY"
        assert result.summary

    def test_the_violations_are_reported_to_the_buyer_not_swallowed(self, plan, assessment, quote):
        """A buyer's agent comparing merchants should be able to see that the
        prose was machine-checked, and what failed."""
        result = compose(plan, assessment, quote,
                         composer=lambda sheet: "The best shoe, only $1.00.")
        assert len(result.grounding.violations) >= 2


class TestFactSheet:
    def test_the_sheet_contains_only_verified_material(self, plan, assessment, quote):
        sheet = rationale.fact_sheet(SHOE, quote, assessment, "PASS", plan)
        assert sheet["price"]["amount"] == quote.amount
        assert all(c["claim"] == "ethical_labour" for c in sheet["verified_claims"])
        assert "internal_cost" not in str(sheet), "commercial data must never reach the composer"

    def test_allowed_numbers_include_prices_and_attributes(self, plan, assessment, quote):
        sheet = rationale.fact_sheet(SHOE, quote, assessment, "PASS", plan)
        allowed = rationale.allowed_numbers(sheet)
        assert 117.79 in allowed
        assert 310 in allowed
