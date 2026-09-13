"""Pricing engine: signal fusion, guardrails, and the outlier filter (§4)."""
from __future__ import annotations

import pytest

from tests.conftest import TEST_SKU


class TestGuardrails:
    """Guardrails run after the learned component and can only override it.
    Each test pins _fair_value so the guardrail under test is isolated from
    the fair-value formula -- otherwise a change to the formula silently
    turns these into tests of nothing."""

    def test_never_prices_below_map(self, pricing_engine):
        # Fair value well below MAP: the MAP floor must win.
        pricing_engine._fair_value = lambda sku: (80.0, 69.44)
        quote = pricing_engine.quote(TEST_SKU)
        assert quote.amount == 119.0
        assert "MAP_FLOOR" in quote.guardrails_applied

    def test_never_prices_below_margin_floor(self, pricing_engine, feature_store):
        # No MAP configured, so only the cost-based margin floor protects us.
        feature_store.write(TEST_SKU, map_price=None)
        pricing_engine._fair_value = lambda sku: (50.0, 69.44)
        quote = pricing_engine.quote(TEST_SKU)
        assert quote.amount == pytest.approx(69.44, abs=0.01)
        assert "MARGIN_FLOOR" in quote.guardrails_applied

    def test_never_prices_above_ceiling(self, pricing_engine):
        pricing_engine._fair_value = lambda sku: (400.0, 69.44)
        quote = pricing_engine.quote(TEST_SKU)
        assert quote.amount == pytest.approx(179.0 * 1.15, abs=0.01)
        assert "PRICE_CEILING" in quote.guardrails_applied

    def test_anti_collusion_nudges_off_an_exact_competitor_match(self, pricing_engine, feature_store):
        feature_store.record_competitor_price(TEST_SKU, "RivalCo", 140.00, accepted=True, reason=None)
        # Fair value chosen so ask == 140.00 exactly, colliding with RivalCo.
        pricing_engine._fair_value = lambda sku: (140.00 / 1.03, 69.44)
        quote = pricing_engine.quote(TEST_SKU)
        assert "ANTI_COLLUSION_JITTER" in quote.guardrails_applied
        assert quote.amount != 140.00

    def test_clean_quote_applies_no_guardrails(self, pricing_engine):
        pricing_engine._fair_value = lambda sku: (140.0, 69.44)
        quote = pricing_engine.quote(TEST_SKU)
        assert quote.guardrails_applied == []
        assert quote.amount == pytest.approx(144.2, abs=0.01)


class TestFairValue:
    def test_tracks_competitor_median_when_the_market_is_visible(self, pricing_engine, feature_store):
        for i, price in enumerate([140.0, 142.0, 144.0]):
            feature_store.record_competitor_price(TEST_SKU, f"c{i}", price, accepted=True, reason=None)
        fair_value, _ = pricing_engine._fair_value(TEST_SKU)
        assert fair_value == pytest.approx(142.0 * 0.98, abs=0.01)

    def test_falls_back_to_list_price_when_blind(self, pricing_engine):
        fair_value, _ = pricing_engine._fair_value(TEST_SKU)
        assert fair_value == pytest.approx(179.0 * 0.85, abs=0.01)

    def test_never_returns_below_the_cost_floor(self, pricing_engine, feature_store):
        # A market that has collapsed below our cost must not drag fair value
        # under the floor -- we decline to compete, we do not sell at a loss.
        for i in range(4):
            feature_store.record_competitor_price(TEST_SKU, f"c{i}", 40.0, accepted=True, reason=None)
        fair_value, cost_floor = pricing_engine._fair_value(TEST_SKU)
        assert fair_value == pytest.approx(cost_floor)

    def test_unknown_sku_raises_rather_than_guessing(self, pricing_engine):
        with pytest.raises(KeyError):
            pricing_engine._fair_value("does-not-exist")


class TestOutlierFilter:
    def test_rejects_a_broken_scrape(self, feature_store):
        for i, price in enumerate([142.0, 138.5]):
            feature_store.ingest_competitor_price(TEST_SKU, f"c{i}", price)
        accepted, reason = feature_store.ingest_competitor_price(TEST_SKU, "OutdoorHub", 6.00)
        assert accepted is False
        assert "deviation" in (reason or "")

    def test_bootstraps_a_new_sku_rather_than_rejecting_everything(self, feature_store):
        accepted, reason = feature_store.ingest_competitor_price("brand-new-sku", "c0", 99.0)
        assert accepted is True and reason is None

    def test_accepts_a_normal_move(self, feature_store):
        for i, price in enumerate([140.0, 142.0, 144.0]):
            feature_store.ingest_competitor_price(TEST_SKU, f"c{i}", price)
        accepted, _ = feature_store.ingest_competitor_price(TEST_SKU, "c9", 150.0)
        assert accepted is True

    def test_rejected_observation_never_enters_the_pricing_window(self, feature_store):
        for i, price in enumerate([142.0, 138.5]):
            feature_store.ingest_competitor_price(TEST_SKU, f"c{i}", price)
        feature_store.ingest_competitor_price(TEST_SKU, "OutdoorHub", 6.00)
        assert 6.00 not in feature_store.competitor_prices(TEST_SKU)

    def test_rejected_observation_is_still_retained_for_audit(self, feature_store):
        for i, price in enumerate([142.0, 138.5]):
            feature_store.ingest_competitor_price(TEST_SKU, f"c{i}", price)
        feature_store.ingest_competitor_price(TEST_SKU, "OutdoorHub", 6.00)
        history = feature_store.offline_history(TEST_SKU)
        assert any(h["price"] == 6.00 and h["accepted"] is False for h in history)


class TestQuoteValidation:
    def test_valid_quote_round_trips(self, pricing_engine):
        quote = pricing_engine.quote(TEST_SKU)
        assert pricing_engine.is_quote_valid(quote.quote_id, quote.sku, quote.amount)

    def test_rejects_a_tampered_amount(self, pricing_engine):
        quote = pricing_engine.quote(TEST_SKU)
        assert not pricing_engine.is_quote_valid(quote.quote_id, quote.sku, 1.00)

    def test_rejects_a_mismatched_sku(self, pricing_engine):
        quote = pricing_engine.quote(TEST_SKU)
        assert not pricing_engine.is_quote_valid(quote.quote_id, "other-sku", quote.amount)

    def test_rejects_an_expired_quote(self, pricing_engine, monkeypatch):
        quote = pricing_engine.quote(TEST_SKU)
        import agentmarket_core.domain.pricing as pricing_module

        monkeypatch.setattr(pricing_module.time, "time", lambda: quote.valid_until + 1)
        assert not pricing_engine.is_quote_valid(quote.quote_id, quote.sku, quote.amount)

    def test_unknown_quote_id_is_invalid(self, pricing_engine):
        assert not pricing_engine.is_quote_valid("q_nope", TEST_SKU, 100.0)


class TestOutcomeFeedback:
    def test_a_win_rewards_realised_margin_not_revenue(self, pricing_engine):
        quote = pricing_engine.quote(TEST_SKU)
        pricing_engine.record_outcome(quote.quote_id, won=True)
        sku, spread, reward = pricing_engine.bandit.updates[-1]
        assert sku == TEST_SKU and spread == quote.spread
        assert reward == pytest.approx(quote.amount - 62.0, abs=0.01)

    def test_a_loss_rewards_nothing(self, pricing_engine):
        quote = pricing_engine.quote(TEST_SKU)
        pricing_engine.record_outcome(quote.quote_id, won=False)
        assert pricing_engine.bandit.updates[-1][2] == 0.0

    def test_publishes_the_outcome_for_downstream_learners(self, pricing_engine, memory_bus):
        quote = pricing_engine.quote(TEST_SKU)
        pricing_engine.record_outcome(quote.quote_id, won=True)
        topics = [e["topic"] for e in memory_bus.recent()]
        assert "pricing.transaction_outcome" in topics
