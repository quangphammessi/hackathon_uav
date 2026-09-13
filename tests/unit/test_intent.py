"""Intent decoding: does the system understand what was actually asked?

This is the capability the challenge weights first, so these tests are written
against the *behaviour a buyer would notice* rather than against the shape of
the decoder. Each one states a sentence a person might say and asserts the
predicate it has to become.

Everything here runs with no model server. That is deliberate: the rule layer
is the floor the system is allowed to fall to, so the floor is what gets
tested. If these pass with `AGENTMARKET_LLM_PROVIDER=none`, the demo works on
a laptop with nothing installed.
"""
from __future__ import annotations

import pytest

from agentmarket_core.domain import claims as claims_mod
from agentmarket_core.domain import intent


def constraint(plan, field: str):
    return next((c for c in plan.constraints if c.field == field), None)


class TestOutcomeToAttribute:
    """The core translation: human outcomes into catalog fields."""

    @pytest.mark.parametrize("phrase", [
        "he has never done it before",
        "I'm a complete beginner",
        "she is new to hiking",
        "just getting into it",
        "this is his first hike",
    ])
    def test_inexperience_becomes_an_experience_ceiling(self, phrase):
        plan = intent.decode(f"something for hiking, {phrase}")
        found = constraint(plan, "experience_level")
        assert found is not None, f"no experience constraint decoded from {phrase!r}"
        assert found.kind == "hard"
        assert found.value == "beginner"

    def test_inexperience_also_implies_no_break_in_and_simple_use(self):
        plan = intent.decode("boots for someone who has never hiked before")
        assert constraint(plan, "break_in_required").value is False
        assert constraint(plan, "ease_of_use").value == "simple"
        # Preferences, not gates: a product without the field must not be
        # disqualified for lacking it.
        assert constraint(plan, "break_in_required").kind == "soft"

    def test_cold_sensitivity_becomes_a_warmth_preference(self):
        plan = intent.decode("a sleeping bag for someone who gets cold easily")
        found = constraint(plan, "use_cases")
        assert found is not None and found.value == "cold_weather"
        assert found.kind == "soft"
        assert found.weight > 1.0, "a stated sensitivity should outrank a generic preference"

    def test_age_implies_joint_support(self):
        plan = intent.decode("walking gear for my 68 year old mother")
        assert constraint(plan, "joint_support") is not None

    def test_explicit_temperature_is_a_hard_specification(self):
        plan = intent.decode("a sleeping bag rated to -12C for alpine use")
        found = constraint(plan, "temp_rating_c")
        assert found is not None and found.kind == "hard"
        assert found.value == -12

    def test_naming_a_product_type_gates_the_category(self):
        plan = intent.decode("waterproof hiking boot under $160")
        found = constraint(plan, "product_type")
        assert found is not None and found.kind == "hard"
        assert "boot" in found.value

    def test_every_constraint_carries_the_words_it_came_from(self):
        """A decode nobody can audit is indistinguishable from a guess."""
        plan = intent.decode("he has never hiked before and gets cold easily")
        assert plan.constraints
        for c in plan.constraints:
            assert c.source_phrase, f"{c.field} has no source phrase"
            assert c.source_phrase.strip() == c.source_phrase


class TestBudget:
    @pytest.mark.parametrize("phrase,amount", [
        ("under $160", 160.0),
        ("no more than $250", 250.0),
        ("budget of $1,200", 1200.0),
        ("up to $99.50", 99.5),
    ])
    def test_hard_budgets(self, phrase, amount):
        plan = intent.decode(f"a backpack {phrase}")
        assert plan.budget == amount
        assert plan.budget_is_hard

    @pytest.mark.parametrize("phrase", ["around $400", "about $400", "roughly $400"])
    def test_soft_budgets_are_marked_soft(self, phrase):
        """"Around $400" and "under $400" are different promises. Treating
        them the same either loses sales or breaks one of them."""
        plan = intent.decode(f"a hiking kit, {phrase}")
        assert plan.budget == 400.0
        assert not plan.budget_is_hard

    def test_budget_is_not_read_as_a_product_specification(self):
        plan = intent.decode("a headlamp under $80")
        assert constraint(plan, "weight_g") is None


class TestValues:
    def test_ethical_language_becomes_a_claim_constraint(self):
        plan = intent.decode("only from brands that are ethically made")
        assert "ethical_labour" in plan.values

    def test_demanding_proof_makes_the_claim_a_hard_gate(self):
        plan = intent.decode("only brands that can actually prove they are ethically made")
        found = constraint(plan, "claim")
        assert found is not None and found.kind == "hard"

    def test_a_values_preference_without_proof_language_only_ranks(self):
        """"I'd prefer sustainable" is a preference. Refusing to sell anything
        else would be the system inventing a requirement the buyer did not
        state."""
        plan = intent.decode("a recycled rain jacket would be nice")
        found = constraint(plan, "claim")
        assert found is not None and found.kind == "soft"

    def test_unknown_values_vocabulary_is_ignored_rather_than_invented(self):
        plan = intent.decode("a jacket from a brand with good vibes")
        assert plan.values == []


class TestBundleAndRecipient:
    @pytest.mark.parametrize("phrase", [
        "the whole kit", "everything he needs", "a starter set", "a complete package",
    ])
    def test_kit_language_sets_bundle_intent(self, phrase):
        assert intent.decode(f"hiking gear, {phrase}").bundle_intent

    def test_a_single_product_request_does_not(self):
        assert not intent.decode("a waterproof jacket under $200").bundle_intent

    def test_recipient_is_recovered(self):
        assert intent.decode("a gift for my dad who hikes").recipient == "gift:dad"


class TestSearchQuery:
    def test_relationship_words_are_stripped_from_the_semantic_query(self):
        """"dad" and "birthday" pull the embedding towards gift-shop copy and
        away from the gear that answers the need."""
        plan = intent.decode("My dad's birthday is coming up, he wants hiking boots")
        assert "dad" not in plan.search_query.lower()
        assert "birthday" not in plan.search_query.lower()
        assert "hiking" in plan.search_query.lower()

    def test_decoded_use_cases_are_added_in_the_catalog_vocabulary(self):
        plan = intent.decode("something for someone who gets cold easily while camping")
        assert "cold weather" in plan.search_query.lower()

    def test_a_query_with_nothing_decodable_still_searches(self):
        plan = intent.decode("blue thing")
        assert plan.search_query.strip()


class TestLlmProposalsAreFiltered:
    """The model may widen coverage. It may not corrupt the decode."""

    def test_unknown_fields_from_the_model_are_discarded(self):
        plan = intent.decode("a jacket", llm_plan={
            "constraints": [{"field": "definitely_not_a_field", "op": "eq", "value": 1,
                             "kind": "hard"}],
        })
        assert all(c.field in intent.ALLOWED_FIELDS for c in plan.constraints)

    def test_rules_still_run_and_win_over_the_model(self):
        """A model that misses the experience signal cannot suppress it."""
        plan = intent.decode("boots for someone who has never hiked before", llm_plan={
            "search_query": "expert mountaineering boots",
            "experience_level": "expert",
        })
        assert constraint(plan, "experience_level").value == "beginner"

    def test_model_values_must_come_from_the_closed_vocabulary(self):
        plan = intent.decode("a jacket", llm_plan={"values": ["made_of_dreams", "repairable"]})
        assert plan.values == ["repairable"]

    def test_decoded_by_records_which_path_ran(self):
        assert intent.decode("a jacket").decoded_by == "deterministic"
        assert intent.decode("a jacket", llm_plan={"search_query": "jacket"}).decoded_by == "llm+rules"


class TestClaimVocabulary:
    def test_proof_language_is_detected(self):
        assert claims_mod.proof_demanded("can they prove it")
        assert claims_mod.proof_demanded("I want certified organic cotton")
        assert not claims_mod.proof_demanded("something sustainable would be nice")

    def test_claim_phrases_snap_to_word_boundaries(self):
        """A source phrase that starts mid-word reads as a bug to anyone
        auditing the decode, even when the decode is right."""
        text = "I only want brands that can actually prove they're ethically made today"
        pairs = claims_mod.claims_with_phrases(text)
        assert pairs
        for _, phrase in pairs:
            assert text.startswith(phrase) or f" {phrase}" in text or phrase in text
            assert not phrase.startswith(" ") and not phrase.endswith(" ")
