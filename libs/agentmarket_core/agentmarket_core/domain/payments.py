"""Payment orchestration + settlement rails (proposal §6.4).

The orchestrator is the last gate before money moves, and it deliberately
re-verifies everything rather than trusting what the storefront told the
agent moments ago:

  1. Intent mandate signature       -- the principal really authorized this agent
  2. Cart mandate signature         -- the cart was not edited after signing
  3. Mandate chain consistency      -- the cart descends from *that* intent
  4. Amount within the intent cap   -- the agent stayed inside its budget
  5. Quote still valid              -- right SKU, right amount, not expired
  6. Trust verification still PASS  -- the credential was not revoked in between

Steps 5 and 6 are the ones people leave out, and they are the ones that
matter most: a quote and a trust token are both *time-varying* facts. A token
can be revoked in the seconds between offer and payment (a recall notice
arrives), and re-checking here is the difference between a system that can
stop a bad sale and one that merely documents it afterwards. Re-verification
is cheap; unwinding a settled payment is not.

Rejections also feed the pricing bandit a `lost` outcome, so the learner sees
the true conversion rate rather than an optimistic one.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from agentmarket_core import db
from agentmarket_core.adapters.bus import EventBus
from agentmarket_core.config import TOPIC_PAYMENT_REJECTED, TOPIC_PAYMENT_SETTLED, settings
from agentmarket_core.domain.mandates import MandateService
from agentmarket_core.models import CartMandate, IntentMandate, OrderResult

log = logging.getLogger("agentmarket.payments")


def settle_card(amount: float, currency: str) -> str:
    """Mock card-network settlement (Visa TAP / Mastercard Agent Pay shape)."""
    time.sleep(0.01)
    return f"card_{uuid.uuid4().hex[:12]}"


def settle_stablecoin_x402(amount: float, currency: str) -> str:
    """Mock x402 stablecoin settlement (HTTP 402 + on-chain facilitator)."""
    time.sleep(0.01)
    return f"x402_{uuid.uuid4().hex[:12]}"


class PaymentOrchestrator:
    def __init__(
        self,
        mandates: MandateService,
        pricing_client,
        verification_client,
        bus: EventBus | None = None,
        persist: bool = True,
    ) -> None:
        self.mandates = mandates
        self.pricing = pricing_client
        self.verification = verification_client
        self.bus = bus
        self.persist = persist

    def execute(
        self, principal_id: str, intent: IntentMandate, cart: CartMandate, agent_id: str | None = None
    ) -> OrderResult:
        reason = self._reject_reason(principal_id, intent, cart)
        if reason:
            result = OrderResult(
                sku=cart.sku, amount=cart.amount, currency=cart.currency,
                status="REJECTED", reason_code=reason,
            )
            # A rejected purchase is still a lost transaction: tell the
            # pricing engine, or the bandit learns from a biased sample.
            self._record_outcomes(cart, won=False)
            self._persist(result, intent, cart, agent_id, principal_id)
            if self.bus:
                self.bus.publish(TOPIC_PAYMENT_REJECTED, {
                    "sku": cart.sku, "amount": cart.amount, "reason_code": reason,
                    "order_id": result.order_id,
                })
            return result

        # Rail selection: below the micropayment threshold, card interchange
        # is a large fraction of the transaction, so an x402 stablecoin
        # transfer is the economically sane rail (proposal §6.4).
        if cart.amount < settings.micropayment_threshold:
            rail, ref = "stablecoin_x402", settle_stablecoin_x402(cart.amount, cart.currency)
        else:
            rail, ref = "card_network", settle_card(cart.amount, cart.currency)

        result = OrderResult(
            sku=cart.sku, amount=cart.amount, currency=cart.currency,
            rail=rail, settlement_ref=ref, status="SETTLED",
        )
        self._record_outcomes(cart, won=True)
        self._persist(result, intent, cart, agent_id, principal_id)
        if self.bus:
            self.bus.publish(TOPIC_PAYMENT_SETTLED, {
                "sku": cart.sku, "amount": cart.amount, "rail": rail,
                "settlement_ref": ref, "order_id": result.order_id,
            })
        return result

    # -- gates --------------------------------------------------------------
    def _reject_reason(self, principal_id: str, intent: IntentMandate, cart: CartMandate) -> str | None:
        if not self.mandates.verify_intent_mandate(intent):
            return "INTENT_MANDATE_SIGNATURE_INVALID"
        if not self.mandates.verify_cart_mandate(principal_id, cart):
            return "CART_MANDATE_SIGNATURE_INVALID"
        if cart.intent_mandate_id != intent.mandate_id or cart.agent_id != intent.agent_id:
            return "MANDATE_CHAIN_MISMATCH"
        if cart.amount > intent.max_amount:
            return "EXCEEDS_INTENT_MANDATE_CAP"

        if cart.items:
            return self._reject_reason_bundle(cart)

        if not self.pricing.is_quote_valid(cart.quote_id, cart.sku, cart.amount):
            return "QUOTE_EXPIRED_OR_MISMATCHED"

        verification = self.verification.verify(cart.trust_token_ref)
        if verification.status != "PASS":
            return verification.reason_code or "VERIFICATION_FAILED"
        return None

    def _reject_reason_bundle(self, cart: CartMandate) -> str | None:
        """Re-validate every line of a multi-item cart.

        Per line, not per total: a bundle is where an expired quote or a
        revoked credential is easiest to hide, because the arithmetic still
        works out. One bad component fails the whole cart -- partially
        settling a kit would leave the buyer with a set that no longer does
        what it was sold to do.
        """
        line_total = round(sum(item.amount for item in cart.items), 2)
        # Bundle pricing discounts the sum, so the cart total must be at or
        # below the line total, never above it.
        if cart.amount > line_total + 0.01:
            return "BUNDLE_TOTAL_EXCEEDS_LINES"

        for item in cart.items:
            if not self.pricing.is_quote_valid(item.quote_id, item.sku, item.amount):
                return "QUOTE_EXPIRED_OR_MISMATCHED"
            verification = self.verification.verify(item.trust_token_ref)
            if verification.status != "PASS":
                return verification.reason_code or "VERIFICATION_FAILED"
        return None

    # -- side effects -------------------------------------------------------
    def _record_outcomes(self, cart: CartMandate, won: bool) -> None:
        """Feed every quote in the cart back to the bandit.

        Every line, not just the headline one: a bundle's components each got
        their own quote from the spread tuner, so reporting only one of them
        teaches the learner about a single SKU and leaves the rest of the kit
        looking like it was never offered at all.
        """
        quote_ids = [item.quote_id for item in cart.items] or [cart.quote_id]
        for quote_id in dict.fromkeys(quote_ids):
            try:
                self.pricing.record_outcome(quote_id, won)
            except Exception:  # noqa: BLE001 -- feedback must never fail a settlement
                log.warning("could not record pricing outcome for quote_id=%s", quote_id, exc_info=True)

    def _persist(self, result: OrderResult, intent, cart, agent_id, principal_id) -> None:
        if not self.persist:
            return
        try:
            db.execute(
                """INSERT INTO orders (order_id, sku, amount, currency, rail, settlement_ref,
                                       status, reason_code, agent_id, principal_id,
                                       intent_mandate, cart_mandate)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
                (result.order_id, result.sku, result.amount, result.currency, result.rail,
                 result.settlement_ref, result.status, result.reason_code,
                 agent_id or cart.agent_id, principal_id,
                 intent.model_dump_json(), cart.model_dump_json()),
            )
        except Exception:  # noqa: BLE001
            log.warning("could not persist order %s", result.order_id, exc_info=True)
