"""Values claims, and the difference between asserting one and proving one.

The challenge brief asks for a way for a brand's unstructured values -- "
sustainability practices, ethical sourcing, long-term durability" -- to be
surfaced so that a buyer who tells their agent "only buy from ethical brands"
gets matched. The obvious implementation is a `sustainable: true` flag in the
catalog, and it is worthless: every merchant sets it to true, so an agent that
trusts it has learned nothing, and an agent that ignores it is back to reading
marketing copy.

What makes a claim decision-grade for a machine is the same thing that makes
it decision-grade for a person: somebody independent checked, and you can see
who, when, and against what. This module answers a values question by looking
for a `certification` event from an auditor in the product's GS1/EPCIS
provenance chain -- the same chain the trust credential is signed over. So a
verified claim inherits every property the credential already has: it is
covered by the event-chain hash, it breaks if the events are back-dated, and
it dies when the credential is revoked.

A claim the merchant asserts with nothing behind it is reported as
ASSERTED_UNATTESTED rather than dropped. That distinction is the product: it
lets a buyer's agent tell a brand that proved it from a brand that typed it.
"""
from __future__ import annotations

import re
from typing import Iterable

from agentmarket_core.models import ClaimVerification

# The vocabulary is closed on purpose. An open-ended claim space cannot be
# audited, and "we accept whatever string the merchant sends" is how a values
# filter becomes decorative.
CLAIM_CATALOG: dict[str, dict] = {
    "ethical_labour": {
        "label": "Ethical labour",
        "description": "Independently audited working conditions and wages at the production site.",
        "synonyms": ["ethical", "ethically made", "ethically sourced", "fair labour", "fair labor",
                     "fair trade", "no sweatshop", "worker", "human rights", "đạo đức"],
    },
    "recycled_materials": {
        "label": "Recycled materials",
        "description": "Verified recycled input content in the finished product.",
        "synonyms": ["recycled", "sustainable", "sustainability", "eco", "eco-friendly",
                     "green", "environment", "environmentally"],
    },
    "repairable": {
        "label": "Repairable",
        "description": "Spare parts and a repair pathway exist, assessed against a repairability scale.",
        "synonyms": ["repairable", "repair", "fixable", "spare parts", "last a long time",
                     "long-lasting", "durable", "durability", "lasts"],
    },
    "low_carbon_transport": {
        "label": "Low-carbon transport",
        "description": "Freight legs verified against a low-emission logistics programme.",
        "synonyms": ["low carbon", "low-carbon", "carbon", "climate", "emissions", "carbon neutral"],
    },
    "pfc_free": {
        "label": "PFC/PFAS-free",
        "description": "No per- or polyfluorinated chemistry in the water-repellent finish.",
        "synonyms": ["pfc", "pfas", "forever chemicals", "non-toxic", "chemical free"],
    },
    "animal_welfare": {
        "label": "Animal welfare",
        "description": "Down or animal-derived material traced to a certified welfare standard.",
        "synonyms": ["animal welfare", "cruelty free", "cruelty-free", "ethical down",
                     "responsible down"],
    },
}

# "prove it" language. When a buyer uses any of these, an unattested claim is
# not a partial match -- it is a fail, and the distinction is the point of the
# whole subsystem.
PROOF_DEMANDED = re.compile(
    r"\b(prove|proven|proof|verif\w*|certif\w*|audit\w*|actually|genuinely|really|"
    r"can show|documented|evidence)\b",
    re.IGNORECASE,
)

_NOTE_CLAIM = re.compile(r"claim\s*=\s*([a-z_]+)", re.IGNORECASE)
_NOTE_CERT = re.compile(r"certificate\s*=\s*([A-Za-z0-9\-]+)", re.IGNORECASE)


def claims_in_text(text: str) -> list[str]:
    """Which values the buyer asked for, by synonym match."""
    return [claim_id for claim_id, _ in claims_with_phrases(text)]


def claims_with_phrases(text: str) -> list[tuple[str, str]]:
    """As `claims_in_text`, but each claim paired with the words that produced
    it, so the resulting constraint can show its own provenance."""
    lowered = (text or "").lower()
    found: list[tuple[str, str]] = []
    for claim_id, meta in CLAIM_CATALOG.items():
        for synonym in meta["synonyms"]:
            index = lowered.find(synonym)
            if index < 0:
                continue
            # Snap the quoted window to word boundaries. A source phrase that
            # starts mid-word ("ually prove they're ethically made") reads as
            # a bug to anyone auditing the decode, even when the decode is
            # right.
            start = max(0, index - 24)
            end = min(len(text), index + len(synonym) + 16)
            if start > 0:
                space = text.find(" ", start)
                start = space + 1 if 0 <= space < index else start
            if end < len(text):
                space = text.rfind(" ", index, end)
                end = space if space > index else end
            found.append((claim_id, text[start:end].strip(" .,;")))
            break
    return found


def proof_demanded(text: str) -> bool:
    return bool(PROOF_DEMANDED.search(text or ""))


def attestations_from_events(events: Iterable[dict]) -> dict[str, dict]:
    """Extract `claim_id -> attestation` from certification events in a chain.

    The attesting party is the event's actor, which is what makes this
    meaningful: the certification event is recorded by the auditor, not by the
    merchant's marketing team, and it is bound into the same event-chain hash
    the credential signs.
    """
    out: dict[str, dict] = {}
    for event in events or []:
        if event.get("biz_step") != "certification":
            continue
        note = event.get("note") or ""
        match = _NOTE_CLAIM.search(note)
        if not match:
            continue
        claim_id = match.group(1).lower()
        if claim_id not in CLAIM_CATALOG:
            continue
        cert = _NOTE_CERT.search(note)
        out[claim_id] = {
            "attested_by": event.get("actor"),
            "certificate": cert.group(1) if cert else None,
            "attested_at": event.get("ts"),
            "evidence_event": {
                "biz_step": event.get("biz_step"),
                "actor": event.get("actor"),
                "location": event.get("location"),
                "ts": event.get("ts"),
            },
        }
    return out


def verify_claims(
    asserted: Iterable[str],
    events: Iterable[dict],
    requested: Iterable[str] | None = None,
) -> list[ClaimVerification]:
    """Reconcile what the merchant asserts against what the chain attests.

    Every claim the buyer asked about is reported, including ones this product
    never claimed (NOT_CLAIMED), because an agent comparing merchants needs
    the absence to be explicit rather than inferred from a shorter list.
    """
    asserted_set = {c for c in (asserted or []) if c in CLAIM_CATALOG}
    attestations = attestations_from_events(events)
    considered = list(dict.fromkeys(list(requested or []) + sorted(asserted_set)))

    results: list[ClaimVerification] = []
    for claim_id in considered:
        meta = CLAIM_CATALOG.get(claim_id)
        if not meta:
            continue
        if claim_id in attestations and claim_id in asserted_set:
            att = attestations[claim_id]
            results.append(ClaimVerification(
                claim=claim_id, label=meta["label"], status="VERIFIED",
                attested_by=att["attested_by"], certificate=att["certificate"],
                attested_at=att["attested_at"], evidence_event=att["evidence_event"],
            ))
        elif claim_id in asserted_set:
            results.append(ClaimVerification(
                claim=claim_id, label=meta["label"], status="ASSERTED_UNATTESTED",
            ))
        else:
            results.append(ClaimVerification(
                claim=claim_id, label=meta["label"], status="NOT_CLAIMED",
            ))
    return results


def verified_claim_ids(claims: Iterable[ClaimVerification]) -> set[str]:
    return {c.claim for c in claims if c.status == "VERIFIED"}
