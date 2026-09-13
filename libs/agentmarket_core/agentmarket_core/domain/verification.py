"""Verification API logic (proposal §6.3).

Returns PASS/FAIL plus a machine-readable reason code -- never a hedge, never
a natural-language explanation an agent would have to parse. An autonomous
buyer needs to branch on the answer, so the answer is an enum.

Checks run cheapest-first and short-circuit, which also happens to be
most-decisive-first: a revoked credential is a definite no regardless of
whether its signature is valid, so there is no reason to verify the signature
of a token we already know is dead.
"""
from __future__ import annotations

import json
import logging

from agentmarket_core.config import settings
from agentmarket_core.domain import provenance
from agentmarket_core.domain.trust_tokens import TrustTokenService
from agentmarket_core.models import VerificationResult

log = logging.getLogger("agentmarket.verification")

REASON_TOKEN_NOT_FOUND = "TOKEN_NOT_FOUND"
REASON_CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"
REASON_SIGNATURE_INVALID = "SIGNATURE_INVALID"
REASON_CHAIN_HASH_MISMATCH = "CHAIN_HASH_MISMATCH"


class VerificationService:
    def __init__(self, token_service: TrustTokenService) -> None:
        self.tokens = token_service

    def verify(self, trust_token_ref: str) -> VerificationResult:
        record = self.tokens.get(trust_token_ref)
        if not record:
            return VerificationResult(
                trust_token_ref=trust_token_ref, status="FAIL", confidence=0.0,
                reason_code=REASON_TOKEN_NOT_FOUND,
            )
        if record["revoked"]:
            return VerificationResult(
                trust_token_ref=trust_token_ref, status="FAIL", confidence=0.0,
                reason_code=REASON_CREDENTIAL_REVOKED,
            )

        credential = record["credential"]
        if isinstance(credential, str):
            credential = json.loads(credential)

        if not self.tokens.verify_credential(credential):
            return VerificationResult(
                trust_token_ref=trust_token_ref, status="FAIL", confidence=0.0,
                reason_code=REASON_SIGNATURE_INVALID,
            )

        # Re-derive the provenance hash from the events as they exist *now*.
        # A valid signature only proves the credential is unmodified; this
        # catches the other direction -- someone editing the underlying event
        # records after a genuine credential was issued over them.
        current = provenance.chain_status(record["sku"], record["batch"])
        if current["event_chain_hash"] != record["event_chain_hash"]:
            return VerificationResult(
                trust_token_ref=trust_token_ref, status="FAIL", confidence=0.0,
                reason_code=REASON_CHAIN_HASH_MISMATCH,
            )

        return VerificationResult(
            trust_token_ref=trust_token_ref,
            status="PASS",
            confidence=max(settings.trust_confidence_threshold, 0.98),
            reason_code=None,
        )
