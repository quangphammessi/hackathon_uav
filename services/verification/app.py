"""Verification service -- Subsystem 3 (proposal §6).

Owns provenance reconciliation, trust-token issuance and the hash-anchored
ledger. It is a separate service for a reason that is about blast radius
rather than scale: it holds the signing key. Keeping it apart means the
storefront -- the component exposed to untrusted agent input -- has no
process-level access to the material that mints trust.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from agentmarket_core.adapters.bus import EventBus
from agentmarket_core.config import settings
from agentmarket_core.domain import provenance
from agentmarket_core.domain.ledger import trust_ledger
from agentmarket_core.domain.trust_tokens import TrustTokenService
from agentmarket_core.domain.verification import VerificationService
from agentmarket_core.models import VerificationResult
from agentmarket_core.service import create_app

log = logging.getLogger("agentmarket.verification.api")

token_service: TrustTokenService | None = None
verification_service: VerificationService | None = None


class SkuRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trust_token_ref: str


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_id: str
    reason: str


class SkuListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skus: list[str]


class ClaimsBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skus: list[str]
    claims: list[str] = []


def _startup(bus: EventBus) -> None:
    global token_service, verification_service
    token_service = TrustTokenService(ledger=trust_ledger, bus=bus)
    verification_service = VerificationService(token_service=token_service)


app, ctx = create_app("verification", on_startup=_startup)


@app.post("/v1/trust-tokens/ensure", tags=["verification"])
def ensure_token(req: SkuRequest) -> dict:
    """Return a live token for the SKU, issuing one if the provenance chain
    supports it. A refusal carries the reason code, which is what the
    storefront turns into an honest rejection."""
    token_id, reason = token_service.ensure_token(req.sku)
    return {"token_id": token_id, "reason_code": reason}


@app.post("/v1/verify", response_model=VerificationResult, tags=["verification"])
def verify(req: VerifyRequest) -> VerificationResult:
    return verification_service.verify(req.trust_token_ref)


@app.post("/v1/verify/skus", tags=["verification"])
def verify_skus(req: SkuListRequest) -> dict:
    """Ensure-and-verify a set of SKUs in one call.

    A bundle has to trust-gate every component before any of it is offered,
    and doing that as two sequential calls per item turns a five-item kit into
    twenty round trips inside a single agent request. The per-SKU answer is
    identical to what the individual endpoints return -- this is a transport
    optimisation, not a weaker check.
    """
    results: dict[str, dict] = {}
    for sku in dict.fromkeys(req.skus):
        token_id, reason = token_service.ensure_token(sku)
        if token_id is None:
            results[sku] = {"trust_token_ref": sku, "status": "FAIL", "confidence": 0.0,
                            "reason_code": reason or "CHAIN_GAP"}
            continue
        results[sku] = verification_service.verify(token_id).model_dump()
    return {"results": results}


@app.post("/v1/claims/batch", tags=["verification"])
def claims_batch(req: ClaimsBatchRequest) -> dict:
    """Resolve values claims for several SKUs at once.

    This is the endpoint that makes "only buy from ethical brands" mean
    something. It answers from certification events in each product's
    provenance chain, so a merchant's own assertion can never produce a
    VERIFIED result -- and an asserted claim with nothing behind it comes back
    as ASSERTED_UNATTESTED rather than being quietly omitted.
    """
    return {
        "results": {
            sku: [c.model_dump() for c in verification_service.claims_for(sku, req.claims)]
            for sku in dict.fromkeys(req.skus)
        }
    }


@app.get("/v1/claims/{sku}", tags=["verification"])
def claims_for_sku(sku: str) -> dict:
    return {"sku": sku,
            "claims": [c.model_dump() for c in verification_service.claims_for(sku)]}


@app.post("/v1/trust-tokens/revoke", tags=["verification"])
def revoke(req: RevokeRequest) -> dict:
    """Revoke a credential -- the recall path. Everything downstream that
    re-verifies (including in-flight payments) starts failing immediately."""
    revoked = token_service.revoke(req.token_id, req.reason)
    if not revoked:
        raise HTTPException(status_code=404, detail="token not found or already revoked")
    return {"revoked": True, "token_id": req.token_id}


@app.get("/v1/trust-tokens/{token_id}", tags=["verification"])
def get_token(token_id: str) -> dict:
    record = token_service.get(token_id)
    if not record:
        raise HTTPException(status_code=404, detail="token not found")
    return record


@app.get("/v1/provenance", tags=["verification"])
def provenance_overview() -> dict:
    return {"skus": provenance.all_chain_statuses()}


@app.get("/v1/provenance/{sku}", tags=["verification"])
def provenance_detail(sku: str) -> dict:
    return provenance.chain_status(sku)


@app.get("/v1/ledger", tags=["verification"])
def ledger(limit: int = 50) -> dict:
    return {"entries": trust_ledger.entries(limit=limit), "stats": trust_ledger.stats()}


@app.get("/v1/ledger/integrity", tags=["verification"])
def ledger_integrity() -> dict:
    """Recompute the entire hash chain. This is the proof that tamper-
    evidence is real rather than asserted."""
    ok, error = trust_ledger.verify_chain_integrity()
    return {"intact": ok, "error": error, "stats": trust_ledger.stats()}


@app.get("/v1/issuer", tags=["verification"])
def issuer() -> dict:
    """The issuer's DID and public key -- what a relying party needs to
    verify a credential without calling us."""
    return {
        "issuer_did": settings.issuer_did,
        "public_key_hex": token_service.public_key_hex(),
        "signature_suite": "Ed25519Signature2020",
    }
