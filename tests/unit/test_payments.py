"""AP2 mandates and the payment orchestrator's gates (§6.4)."""
from __future__ import annotations

import pytest

from agentmarket_core.domain.mandates import MandateService
from agentmarket_core.domain.payments import PaymentOrchestrator
from agentmarket_core.models import VerificationResult
from tests.conftest import TEST_SKU


class FakePricing:
    """Stands in for the pricing service. Tests flip `valid` to exercise the
    quote gate without needing a real engine or a real clock."""

    def __init__(self, valid: bool = True) -> None:
        self.valid = valid
        self.outcomes: list[tuple[str, bool]] = []

    def is_quote_valid(self, quote_id, sku, amount) -> bool:
        return self.valid

    def record_outcome(self, quote_id, won) -> None:
        self.outcomes.append((quote_id, won))


class FakeVerification:
    def __init__(self, status: str = "PASS", reason: str | None = None) -> None:
        self.status = status
        self.reason = reason

    def verify(self, ref) -> VerificationResult:
        return VerificationResult(
            trust_token_ref=ref, status=self.status,
            confidence=0.98 if self.status == "PASS" else 0.0, reason_code=self.reason,
        )


@pytest.fixture
def stack():
    mandates = MandateService()
    pricing = FakePricing()
    verification = FakeVerification()
    orchestrator = PaymentOrchestrator(
        mandates=mandates, pricing_client=pricing,
        verification_client=verification, persist=False,
    )
    return mandates, pricing, verification, orchestrator


def make_chain(mandates, amount=150.0, cap=200.0, principal="p1", agent="a1"):
    intent = mandates.create_intent_mandate(principal, agent, "buy boots", cap)
    cart = mandates.create_cart_mandate(principal, intent, TEST_SKU, "q_1", amount, "vc_1")
    return intent, cart


class TestMandateSignatures:
    def test_intent_round_trips(self, stack):
        mandates, *_ = stack
        intent, _ = make_chain(mandates)
        assert mandates.verify_intent_mandate(intent) is True

    def test_cart_round_trips(self, stack):
        mandates, *_ = stack
        _, cart = make_chain(mandates)
        assert mandates.verify_cart_mandate("p1", cart) is True

    def test_tampering_with_the_amount_breaks_the_signature(self, stack):
        mandates, *_ = stack
        _, cart = make_chain(mandates)
        cart.amount = 1.00  # pay $1 for a $150 cart, after signing
        assert mandates.verify_cart_mandate("p1", cart) is False

    def test_tampering_with_the_sku_breaks_the_signature(self, stack):
        mandates, *_ = stack
        _, cart = make_chain(mandates)
        cart.sku = "some-cheaper-thing"
        assert mandates.verify_cart_mandate("p1", cart) is False

    def test_another_principal_cannot_verify_it(self, stack):
        mandates, *_ = stack
        _, cart = make_chain(mandates)
        assert mandates.verify_cart_mandate("someone_else", cart) is False


class TestSettlementGates:
    def test_settles_when_every_gate_passes(self, stack):
        mandates, _, _, orchestrator = stack
        intent, cart = make_chain(mandates)
        result = orchestrator.execute("p1", intent, cart)
        assert result.status == "SETTLED"
        assert result.settlement_ref

    def test_rejects_a_tampered_cart(self, stack):
        mandates, _, _, orchestrator = stack
        intent, cart = make_chain(mandates)
        cart.amount = 1.00
        result = orchestrator.execute("p1", intent, cart)
        assert result.status == "REJECTED"
        assert result.reason_code == "CART_MANDATE_SIGNATURE_INVALID"

    def test_rejects_when_over_the_intent_cap(self, stack):
        mandates, _, _, orchestrator = stack
        intent, cart = make_chain(mandates, amount=500.0, cap=200.0)
        result = orchestrator.execute("p1", intent, cart)
        assert result.reason_code == "EXCEEDS_INTENT_MANDATE_CAP"

    def test_rejects_a_cart_from_a_different_intent(self, stack):
        mandates, _, _, orchestrator = stack
        intent_a, _ = make_chain(mandates)
        intent_b, cart_b = make_chain(mandates)
        result = orchestrator.execute("p1", intent_a, cart_b)
        assert result.reason_code == "MANDATE_CHAIN_MISMATCH"

    def test_rejects_an_expired_or_mismatched_quote(self, stack):
        mandates, pricing, _, orchestrator = stack
        pricing.valid = False
        intent, cart = make_chain(mandates)
        result = orchestrator.execute("p1", intent, cart)
        assert result.reason_code == "QUOTE_EXPIRED_OR_MISMATCHED"

    def test_rejects_when_trust_verification_fails(self, stack):
        mandates, _, verification, orchestrator = stack
        verification.status, verification.reason = "FAIL", "CREDENTIAL_REVOKED"
        intent, cart = make_chain(mandates)
        result = orchestrator.execute("p1", intent, cart)
        assert result.status == "REJECTED"
        assert result.reason_code == "CREDENTIAL_REVOKED"

    def test_a_revocation_between_offer_and_payment_blocks_settlement(self, stack):
        """The recall scenario: everything was fine when the offer was made,
        and the credential is pulled before the agent pays."""
        mandates, _, verification, orchestrator = stack
        intent, cart = make_chain(mandates)
        verification.status, verification.reason = "FAIL", "CREDENTIAL_REVOKED"
        assert orchestrator.execute("p1", intent, cart).status == "REJECTED"


class TestRailSelection:
    def test_small_amounts_route_to_the_stablecoin_rail(self, stack):
        mandates, _, _, orchestrator = stack
        intent, cart = make_chain(mandates, amount=9.99, cap=100.0)
        result = orchestrator.execute("p1", intent, cart)
        assert result.status == "SETTLED"
        assert result.rail == "stablecoin_x402"

    def test_larger_amounts_route_to_the_card_network(self, stack):
        mandates, _, _, orchestrator = stack
        intent, cart = make_chain(mandates, amount=150.0)
        assert orchestrator.execute("p1", intent, cart).rail == "card_network"


class TestOutcomeFeedback:
    def test_a_settlement_reports_a_win(self, stack):
        mandates, pricing, _, orchestrator = stack
        intent, cart = make_chain(mandates)
        orchestrator.execute("p1", intent, cart)
        assert pricing.outcomes[-1][1] is True

    def test_a_rejection_still_reports_a_loss(self, stack):
        """Otherwise the bandit only ever learns from successful quotes and
        systematically overestimates conversion."""
        mandates, pricing, verification, orchestrator = stack
        verification.status, verification.reason = "FAIL", "TOKEN_NOT_FOUND"
        intent, cart = make_chain(mandates)
        orchestrator.execute("p1", intent, cart)
        assert pricing.outcomes[-1][1] is False
