"""The "why": a justification the buyer's agent can check, not just read.

The brief asks the merchant to return "not just a list of SKUs, but a logical
justification of why these products match the buyer's specific intention". It
also names the failure mode that makes this hard: today's product data leads
to "hallucinated product claims". Those two requirements pull against each
other -- the fluent, persuasive pitch is exactly the thing a language model
will happily invent, and a merchant system that answers a machine buyer with
invented specifications is worse than one that stays silent, because the
buyer's agent has no way to tell.

The resolution here is that the model never supplies facts, only sentences,
and the sentences are checked before they ship:

1. A **fact sheet** is assembled from verified sources only -- the product
   store, the pricing engine's quote, the matcher's per-requirement evidence,
   and the verification service's claim results.
2. The model is asked to write a short justification *from that sheet*.
3. Every number in what it wrote must appear in the sheet. Every values word
   ("ethical", "recycled", "sustainable") must correspond to a claim the
   provenance chain actually attests. Unverifiable superlatives are rejected
   outright.
4. On any violation the model's text is discarded and a deterministic template
   ships instead, and `GroundingReport` on the offer says which one the buyer
   received and what the violations were.

So the worst case is a plainer sentence, never a false one. And because the
template path is complete on its own, the whole system still produces a proper
justification with no model server running at all.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from agentmarket_core.domain.claims import CLAIM_CATALOG
from agentmarket_core.models import (
    Bundle,
    CandidateAssessment,
    ClaimVerification,
    GroundingReport,
    IntentPlan,
    OfferRationale,
    PriceQuote,
    ProductSpec,
    RequirementMatch,
)

log = logging.getLogger("agentmarket.rationale")

# Standalone numbers only: digits that are not glued to letters or hyphens.
#
# The loose version of this pattern had a hole worth keeping a note about. A
# certificate reference like "FLA-2026-0001" contributed `2026` and `1` to the
# set of numbers the composer was allowed to use, which meant a model could
# write "only $1.00" and pass the grounding check on the strength of a
# certificate serial. Identifier digits are not facts about the price, the
# weight or the rating, so they must not authorise numeric claims. The same
# pattern is used on both sides -- the fact sheet and the model's text -- so
# that "IPX8" never contributes a bare `8` to either.
_NUMBER = re.compile(r"(?<![A-Za-z0-9.\-])\d+(?:[.,]\d+)*(?![A-Za-z0-9\-])")

# Claims that cannot be checked against anything. A merchant system may not
# tell a buyer's agent that a product is "the best" or "guaranteed" -- there
# is no field in the catalog that could make it true.
UNVERIFIABLE = re.compile(
    r"\b(best|cheapest|finest|number one|no\.? ?1|unbeatable|guarantee[ds]?|"
    r"perfect|ultimate|world[- ]class|award[- ]winning|market[- ]leading|"
    r"lowest price|top[- ]rated)\b",
    re.IGNORECASE,
)


def _numbers_in(value: Any) -> set[float]:
    out: set[float] = set()
    if isinstance(value, bool) or value is None:
        return out
    if isinstance(value, (int, float)):
        out.add(round(float(value), 2))
        return out
    if isinstance(value, dict):
        for key, item in value.items():
            out |= _numbers_in(key)
            out |= _numbers_in(item)
        return out
    if isinstance(value, (list, tuple, set)):
        for item in value:
            out |= _numbers_in(item)
        return out
    for token in _NUMBER.findall(str(value)):
        try:
            out.add(round(float(token.replace(",", "")), 2))
        except ValueError:
            continue
    return out


def fact_sheet(
    spec: ProductSpec,
    quote: PriceQuote,
    assessment: CandidateAssessment,
    trust_status: str,
    plan: IntentPlan,
    bundle: Bundle | None = None,
) -> dict:
    """Everything the composer is allowed to know. Nothing else may appear in
    the output, which is what makes the grounding check decidable."""
    sheet = {
        "product": {"sku": spec.sku, "name": spec.name, "category": spec.category},
        "attributes": dict(spec.attributes or {}),
        "price": {
            "amount": quote.amount, "currency": quote.currency,
            "fair_value": quote.fair_value, "valid_seconds": round(quote.valid_until - quote.issued_at),
        },
        "buyer_budget": plan.budget,
        "interpreted_need": plan.interpreted_need,
        "trust": {"status": trust_status},
        "requirements_met": [
            {"requirement": m.requirement, "evidence": m.evidence, "from_phrase": m.source_phrase}
            for m in assessment.matched
        ],
        "requirements_not_met": [
            {"requirement": m.requirement, "evidence": m.evidence} for m in assessment.unmet
        ],
        "verified_claims": [
            {"claim": c.claim, "label": c.label, "attested_by": c.attested_by,
             "certificate": c.certificate}
            for c in assessment.claims if c.status == "VERIFIED"
        ],
        "unattested_claims": [
            {"claim": c.claim, "label": c.label}
            for c in assessment.claims if c.status == "ASSERTED_UNATTESTED"
        ],
    }
    if bundle:
        sheet["bundle"] = {
            "item_count": len(bundle.items),
            "subtotal": bundle.subtotal,
            "discount": bundle.bundle_discount,
            "total": bundle.total,
            "items": [{"sku": i.sku, "name": i.name, "amount": i.price.amount,
                       "role_in_bundle": i.role_in_bundle} for i in bundle.items],
        }
    return sheet


def allowed_numbers(sheet: dict) -> set[float]:
    return _numbers_in(sheet)


def check_grounding(text: str, sheet: dict) -> list[str]:
    """Return the list of violations; empty means the text may ship."""
    violations: list[str] = []
    allowed = allowed_numbers(sheet)

    for token in _NUMBER.findall(text or ""):
        try:
            value = round(float(token.replace(",", "")), 2)
        except ValueError:
            continue
        if value in allowed:
            continue
        # Allow a rounded restatement of a permitted figure ($112.94 -> $113).
        if any(abs(value - a) <= 0.5 and a != 0 for a in allowed):
            continue
        violations.append(f"number {token} does not appear in the verified fact sheet")

    verified = {c["claim"] for c in sheet.get("verified_claims", [])}
    lowered = (text or "").lower()
    for claim_id, meta in CLAIM_CATALOG.items():
        if claim_id in verified:
            continue
        for synonym in meta["synonyms"]:
            # Short synonyms produce false positives inside ordinary words
            # ("green" in "evergreen"); require a word boundary.
            if len(synonym) < 4:
                continue
            if re.search(rf"\b{re.escape(synonym)}\b", lowered):
                violations.append(
                    f"claims '{synonym}' but {meta['label']} is not attested for this product"
                )
                break

    superlative = UNVERIFIABLE.search(text or "")
    if superlative:
        violations.append(f"unverifiable superlative '{superlative.group(0)}'")

    return violations


# ---------------------------------------------------------------------------
# Deterministic composition -- the floor, and the thing that ships when the
# model is absent or wrong. It is written to be genuinely persuasive on its
# own, because "the fallback is fine" has to be true for the guard above to be
# a real choice rather than a threat.
# ---------------------------------------------------------------------------

def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v).replace("_", " ") for v in value)
    return str(value)


def _phrase_for(match: RequirementMatch) -> str:
    if match.field == "claim":
        return f"{match.requirement} ({match.evidence})"
    return f"{match.requirement} (this one is {_render(match.actual_value)})"


def _join(items: Iterable[str]) -> str:
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def compose_template(
    spec: ProductSpec,
    quote: PriceQuote,
    assessment: CandidateAssessment,
    plan: IntentPlan,
    bundle: Bundle | None = None,
) -> str:
    reasons = [_phrase_for(m) for m in assessment.matched[:3]]
    lead = (f"{spec.name} answers the decoded need"
            if reasons else f"{spec.name} is the closest available match")
    body = f"{lead} on {_join(reasons)}." if reasons else f"{lead}."

    verified = [c for c in assessment.claims if c.status == "VERIFIED"]
    if verified:
        attested = _join([f"{c.label.lower()}, attested by {c.attested_by}" for c in verified[:2]])
        body += (f" The values requirement is met with evidence rather than assertion: "
                 f"{attested}, recorded in the product's provenance chain and covered by "
                 f"its signed credential.")

    if bundle:
        parts = _join([f"{i.name} ({i.role_in_bundle})" for i in bundle.items])
        body += (f" Proposed as a {len(bundle.items)}-item kit -- {parts} -- for "
                 f"${bundle.total:,.2f} after a ${bundle.bundle_discount:,.2f} bundle adjustment.")
    else:
        body += f" Quoted at ${quote.amount:,.2f} {quote.currency}"
        if plan.budget:
            headroom = plan.budget - quote.amount
            if headroom >= 0:
                body += f", ${headroom:,.2f} inside the stated budget."
            else:
                body += f", ${abs(headroom):,.2f} above the stated budget."
        else:
            body += "."

    if "NEGOTIATED_CONCESSION" in (quote.guardrails_applied or []) or \
            "BUDGET_CONCESSION" in ((bundle.guardrails_applied if bundle else []) or []):
        body += (" That price already includes an automatic adjustment made to meet the "
                 "stated budget; it is the merchant's opening position, not its floor.")

    return body


def compose(
    spec: ProductSpec,
    quote: PriceQuote,
    assessment: CandidateAssessment,
    plan: IntentPlan,
    trust_status: str,
    rejected: list[CandidateAssessment] | None = None,
    bundle: Bundle | None = None,
    composer=None,
) -> OfferRationale:
    """Build the justification, preferring the model and falling back safely.

    `composer` is injected (rather than imported) so the storefront can pass
    the LLM-backed composer while tests can pass one that deliberately
    hallucinates -- which is the only way to prove the guard works.
    """
    sheet = fact_sheet(spec, quote, assessment, trust_status, plan, bundle)
    template = compose_template(spec, quote, assessment, plan, bundle)

    summary = template
    grounding = GroundingReport(status="TEMPLATE_ONLY", composed_by="template",
                                checked_numbers=[], violations=[])

    if composer is not None:
        try:
            candidate = (composer(sheet) or "").strip()
        except Exception as exc:  # noqa: BLE001 -- a composer failure must never fail the offer
            log.warning("rationale composer failed (%s); using template", exc)
            candidate = ""
        if candidate:
            violations = check_grounding(candidate, sheet)
            numbers = _NUMBER.findall(candidate)
            if violations:
                log.warning("rationale rejected by grounding check: %s", violations)
                grounding = GroundingReport(
                    status="TEMPLATE_FALLBACK", composed_by="template",
                    checked_numbers=numbers, violations=violations,
                )
            else:
                summary = candidate
                grounding = GroundingReport(
                    status="VERIFIED", composed_by="llm",
                    checked_numbers=numbers, violations=[],
                )

    near_misses = [
        {"sku": r.sku, "name": r.name,
         "reason": r.disqualified_by or (r.unmet[0].requirement if r.unmet else "lower fit score"),
         "fit_score": r.fit_score}
        for r in (rejected or [])[:3]
    ]

    from agentmarket_core.domain.matching import tradeoffs as _tradeoffs

    return OfferRationale(
        interpreted_need=plan.interpreted_need,
        summary=summary,
        matched=assessment.matched,
        tradeoffs=_tradeoffs(assessment),
        verified_claims=[c for c in assessment.claims if c.status != "NOT_CLAIMED"],
        rejected_alternatives=near_misses,
        grounding=grounding,
    )


def claims_summary(claims: Iterable[ClaimVerification]) -> dict:
    claims = list(claims)
    return {
        "verified": [c.claim for c in claims if c.status == "VERIFIED"],
        "asserted_unattested": [c.claim for c in claims if c.status == "ASSERTED_UNATTESTED"],
    }
