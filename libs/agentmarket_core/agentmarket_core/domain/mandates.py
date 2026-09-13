"""AP2-style Intent and Cart Mandates (proposal §6.4).

AP2's model is a chain of signed authorizations: the human principal signs an
Intent Mandate ("you may spend up to $200 on hiking boots"), and the agent's
specific choice is bound by a Cart Mandate ("this exact SKU, this exact quote,
this exact amount"). The payment orchestrator verifies both before money
moves, so it never has to take the agent's word for what it was authorized to
do.

Each principal gets their own Ed25519 keypair. In production these are held
by the principal's wallet/issuer and this service would only ever see public
keys; keeping the private half here is the demo's one concession, and it is
isolated to this class so replacing it is a single-file change.

`validate_assignment=True` on the models means tampering with a mandate after
signing (`cart.amount = 1.00`) is still structurally valid pydantic -- which
is the point: the signature, not the type system, is what catches it.
"""
from __future__ import annotations

import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from agentmarket_core.models import CartItem, CartMandate, IntentMandate


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class MandateService:
    def __init__(self) -> None:
        self._keys: dict[str, ed25519.Ed25519PrivateKey] = {}

    def _key_for(self, principal_id: str) -> ed25519.Ed25519PrivateKey:
        if principal_id not in self._keys:
            self._keys[principal_id] = ed25519.Ed25519PrivateKey.generate()
        return self._keys[principal_id]

    def public_key_hex(self, principal_id: str) -> str:
        from cryptography.hazmat.primitives import serialization

        return self._key_for(principal_id).public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        ).hex()

    # -- intent -------------------------------------------------------------
    @staticmethod
    def _intent_payload(m: IntentMandate) -> dict:
        return {
            "mandate_id": m.mandate_id, "agent_id": m.agent_id, "principal_id": m.principal_id,
            "instructions": m.instructions, "max_amount": m.max_amount,
            "currency": m.currency, "issued_at": m.issued_at,
        }

    def create_intent_mandate(
        self, principal_id: str, agent_id: str, instructions: str, max_amount: float
    ) -> IntentMandate:
        mandate = IntentMandate(
            agent_id=agent_id, principal_id=principal_id, instructions=instructions,
            max_amount=max_amount, signature="",
        )
        mandate.signature = base64.b64encode(
            self._key_for(principal_id).sign(_canonical(self._intent_payload(mandate)))
        ).decode()
        return mandate

    def verify_intent_mandate(self, mandate: IntentMandate) -> bool:
        try:
            self._key_for(mandate.principal_id).public_key().verify(
                base64.b64decode(mandate.signature), _canonical(self._intent_payload(mandate))
            )
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False

    # -- cart ---------------------------------------------------------------
    @staticmethod
    def _cart_payload(m: CartMandate) -> dict:
        return {
            "mandate_id": m.mandate_id, "intent_mandate_id": m.intent_mandate_id,
            "agent_id": m.agent_id, "sku": m.sku, "quote_id": m.quote_id,
            "amount": m.amount, "currency": m.currency,
            "trust_token_ref": m.trust_token_ref, "signed_at": m.signed_at,
            # Lines are inside the signature, so a bundle cart cannot have an
            # item swapped or a line's price edited after the principal signed
            # it. Signing only the total would authorise the number while
            # leaving what it buys unbound.
            "items": [
                {"sku": i.sku, "quote_id": i.quote_id, "amount": i.amount,
                 "trust_token_ref": i.trust_token_ref}
                for i in m.items
            ],
            "bundle_id": m.bundle_id,
        }

    def create_cart_mandate(
        self, principal_id: str, intent_mandate: IntentMandate, sku: str,
        quote_id: str, amount: float, trust_token_ref: str,
        items: list[CartItem] | None = None, bundle_id: str | None = None,
    ) -> CartMandate:
        mandate = CartMandate(
            intent_mandate_id=intent_mandate.mandate_id, agent_id=intent_mandate.agent_id,
            sku=sku, quote_id=quote_id, amount=amount, trust_token_ref=trust_token_ref,
            items=list(items or []), bundle_id=bundle_id,
            signature="",
        )
        mandate.signature = base64.b64encode(
            self._key_for(principal_id).sign(_canonical(self._cart_payload(mandate)))
        ).decode()
        return mandate

    def verify_cart_mandate(self, principal_id: str, mandate: CartMandate) -> bool:
        try:
            self._key_for(principal_id).public_key().verify(
                base64.b64decode(mandate.signature), _canonical(self._cart_payload(mandate))
            )
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False
