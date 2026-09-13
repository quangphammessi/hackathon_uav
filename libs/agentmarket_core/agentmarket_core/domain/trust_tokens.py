"""Trust-token issuance: W3C-Verifiable-Credential-shaped, Ed25519-signed
(proposal §6.2).

The signing key is loaded from `SIGNING_KEY_PATH` and created on first use if
absent. Persisting it is not a convenience -- an ephemeral per-process key
means a token issued by one replica cannot be verified by another, and the
whole system fails closed with SIGNATURE_INVALID the moment you run more than
one instance. In production this file is the mount point for a KMS/HSM-backed
key; the interface (`sign`/`public_key`) is the same either way, which is why
swapping it does not touch issuance logic.

Ed25519 rather than RSA: 64-byte signatures, sub-millisecond verify, and no
parameter choices to get wrong.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature

from agentmarket_core import db
from agentmarket_core.adapters.bus import EventBus
from agentmarket_core.config import TOPIC_TRUST_ISSUED, TOPIC_TRUST_REVOKED, settings
from agentmarket_core.domain import provenance
from agentmarket_core.domain.ledger import TrustLedger, trust_ledger
from agentmarket_core.models import TrustToken

log = logging.getLogger("agentmarket.trust")


def _canonical(payload: dict) -> bytes:
    """Signatures must be computed over bytes that both parties derive the
    same way, so the payload is serialized canonically (sorted keys, no
    incidental whitespace) rather than however json.dumps felt that day."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def load_or_create_signing_key(path: str | None) -> ed25519.Ed25519PrivateKey:
    if path is None:
        return ed25519.Ed25519PrivateKey.generate()  # tests: isolated, ephemeral
    key_path = Path(path)
    if key_path.exists():
        raw = json.loads(key_path.read_text())
        return ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw["ed25519_private_key_hex"]))
    key = ed25519.Ed25519PrivateKey.generate()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(json.dumps({
        "ed25519_private_key_hex": key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ).hex(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "note": "Demo key. In production this is a KMS/HSM handle, not a file.",
    }))
    os.chmod(key_path, 0o600)
    log.info("generated new Ed25519 signing key at %s", key_path)
    return key


class TrustTokenService:
    def __init__(
        self,
        ledger: TrustLedger | None = None,
        bus: EventBus | None = None,
        keys_path: str | None = "__default__",
    ) -> None:
        self.ledger = ledger or trust_ledger
        self.bus = bus
        resolved = settings.signing_key_path if keys_path == "__default__" else keys_path
        self._key = load_or_create_signing_key(resolved)

    def public_key_hex(self) -> str:
        return self._key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        ).hex()

    def _sign(self, payload: dict) -> str:
        return base64.b64encode(self._key.sign(_canonical(payload))).decode()

    def _verify_signature(self, payload: dict, signature_b64: str) -> bool:
        try:
            self._key.public_key().verify(base64.b64decode(signature_b64), _canonical(payload))
            return True
        except (InvalidSignature, ValueError):
            return False

    # -- issuance -----------------------------------------------------------
    def issue(self, sku: str, batch: str | None = None) -> tuple[TrustToken | None, str | None]:
        """Issue a credential for (sku, batch), or refuse with a reason code.

        Refusing is the interesting path: a gap in the provenance chain
        produces CHAIN_GAP and no token, which is what makes the downstream
        verification gate meaningful rather than decorative.
        """
        status = provenance.chain_status(sku, batch)
        if not status["ready"]:
            return None, status["reason_code"] or "CHAIN_GAP"

        product = db.query_one("SELECT gtin FROM products WHERE sku = %s", (sku,))
        if not product:
            return None, "UNKNOWN_SKU"

        gtin = product["gtin"]
        # GS1 Digital Link: the resolvable identifier an agent can dereference
        # to find this credential (proposal §6.2).
        gs1_link = f"https://id.gs1.org/01/{gtin}/10/{status['batch']}"
        issuance_date = datetime.now(timezone.utc).isoformat()

        credential_subject = {
            "id": gs1_link,
            "sku": sku,
            "gtin": gtin,
            "batch": status["batch"],
            "provenanceComplete": True,
            "verifiedSteps": status["present_steps"],
        }
        # The signature covers the chain hash, so the credential is bound to
        # the exact provenance events that justified issuing it.
        signing_payload = {
            "issuer": settings.issuer_did,
            "issuanceDate": issuance_date,
            "credentialSubject": credential_subject,
            "eventChainHash": status["event_chain_hash"],
        }
        token = TrustToken(
            issuer=settings.issuer_did,
            issuance_date=issuance_date,
            gs1_digital_link=gs1_link,
            credential_subject=credential_subject,
            event_chain_hash=status["event_chain_hash"],
            proof={
                "type": "Ed25519Signature2020",
                "created": issuance_date,
                "verificationMethod": f"{settings.issuer_did}#key-1",
                "proofPurpose": "assertionMethod",
                "proofValue": self._sign(signing_payload),
            },
        )

        db.execute(
            """INSERT INTO trust_tokens (token_id, sku, batch, gs1_digital_link, issuer,
                                         issuance_date, event_chain_hash, credential)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (token.token_id, sku, status["batch"], gs1_link, settings.issuer_did,
             issuance_date, status["event_chain_hash"], token.model_dump_json()),
        )
        self.ledger.append("ISSUE", token.token_id, {
            "sku": sku, "batch": status["batch"], "gs1_digital_link": gs1_link,
            "event_chain_hash": status["event_chain_hash"], "issuer": settings.issuer_did,
        })
        if self.bus:
            self.bus.publish(TOPIC_TRUST_ISSUED, {
                "sku": sku, "token_id": token.token_id, "batch": status["batch"],
                "gs1_digital_link": gs1_link,
            })
        return token, None

    def ensure_token(self, sku: str) -> tuple[str | None, str | None]:
        """Return a live token for the SKU, issuing one only if needed."""
        row = db.query_one(
            "SELECT token_id FROM trust_tokens WHERE sku = %s AND NOT revoked "
            "ORDER BY issuance_date DESC LIMIT 1",
            (sku,),
        )
        if row:
            return row["token_id"], None
        token, reason = self.issue(sku)
        return (token.token_id if token else None), reason

    def get(self, token_id: str) -> dict | None:
        return db.query_one(
            "SELECT token_id, sku, batch, gs1_digital_link, issuer, event_chain_hash, "
            "credential, revoked, revoked_reason FROM trust_tokens WHERE token_id = %s",
            (token_id,),
        )

    def revoke(self, token_id: str, reason: str) -> bool:
        updated = db.execute(
            "UPDATE trust_tokens SET revoked = TRUE, revoked_reason = %s, revoked_at = now() "
            "WHERE token_id = %s AND NOT revoked",
            (reason, token_id),
        )
        if not updated:
            return False
        self.ledger.append("REVOKE", token_id, {"reason": reason})
        if self.bus:
            self.bus.publish(TOPIC_TRUST_REVOKED, {"token_id": token_id, "reason": reason})
        return True

    def verify_credential(self, credential: dict) -> bool:
        """Re-derive the signing payload from the stored credential and check
        the signature -- the check a relying party would perform."""
        try:
            payload = {
                "issuer": credential["issuer"],
                "issuanceDate": credential["issuance_date"],
                "credentialSubject": credential["credential_subject"],
                "eventChainHash": credential["event_chain_hash"],
            }
            return self._verify_signature(payload, credential["proof"]["proofValue"])
        except (KeyError, TypeError):
            return False
