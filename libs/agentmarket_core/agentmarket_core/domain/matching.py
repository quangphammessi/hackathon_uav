"""Scoring a candidate against a decoded intent, with the evidence kept.

Retrieval gives you *similar*. This module decides *suitable*, which is a
different question and the one the buyer actually asked. A vector search for a
beginner's first hiking kit will happily rank an expert alpine sleeping bag
highly, because the words around it are all about hiking and cold. The
predicate layer is what stops that becoming a recommendation.

Two properties are deliberate:

**Every check is recorded, satisfied or not.** The matcher returns the full
`RequirementMatch` list rather than a boolean, so the offer can say "waterproof
IPX4+: yes, this is IPX6" and the rejection can say which requirement no
product met. Scores are not explanations; a number cannot be argued with,
while a list of checked predicates can.

**Hard and soft do different jobs.** A hard predicate disqualifies. A soft one
moves the ranking. Collapsing them either throws away good products over a
preference or ships a product that violates a stated requirement, and both are
worse than being told no.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from agentmarket_core.domain.claims import CLAIM_CATALOG
from agentmarket_core.domain.intent import EASE_SCALE, EXPERIENCE_SCALE
from agentmarket_core.models import (
    CandidateAssessment,
    ClaimVerification,
    IntentConstraint,
    IntentPlan,
    ProductSpec,
    RequirementMatch,
)

# The quoted price usually lands below list, so filtering candidates on list
# price against a hard budget would discard products that end up affordable.
# Candidates get slack here; the real budget check runs on the quote.
BUDGET_SLACK = 1.25

_IPX = re.compile(r"IPX(\d)", re.IGNORECASE)

SCALES = {"experience_level": EXPERIENCE_SCALE, "ease_of_use": EASE_SCALE}


def _attribute(spec: ProductSpec, field: str) -> Any:
    if field == "category":
        return spec.category
    return (spec.attributes or {}).get(field)


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        ipx = _IPX.fullmatch(value.strip())
        if ipx:
            return float(ipx.group(1))
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _describe(field: str, value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v).replace("_", " ") for v in value)
    return str(value)


def _label(constraint: IntentConstraint) -> str:
    field = constraint.field.replace("_", " ")
    value = constraint.value
    if constraint.field == "claim":
        meta = CLAIM_CATALOG.get(str(value), {})
        return f"independently verified {meta.get('label', value).lower()}"
    if constraint.field == "use_cases":
        return f"suitable for {str(value).replace('_', ' ')}"
    words = {"lte": "at most", "gte": "at least", "eq": "is", "not_eq": "is not",
             "contains": "includes", "in": "one of"}
    if isinstance(value, bool):
        return f"{field}: {'required' if value else 'not required'}"
    return f"{field} {words.get(constraint.op, constraint.op)} {_describe(constraint.field, value)}"


def _satisfies(constraint: IntentConstraint, actual: Any) -> bool | None:
    """True/False, or None when the product simply does not carry the field.

    Absent is not the same as failing. A daypack has no `temp_rating_c` and
    should not be disqualified by a warmth preference that does not apply to
    it; it should just not earn the point.
    """
    if actual is None:
        return None
    op, expected = constraint.op, constraint.value

    if op == "contains":
        if isinstance(actual, (list, tuple, set)):
            return str(expected) in {str(a) for a in actual}
        return str(expected).lower() in str(actual).lower()

    if op == "in":
        allowed = expected if isinstance(expected, (list, tuple, set)) else [expected]
        return str(actual) in {str(a) for a in allowed}

    scale = SCALES.get(constraint.field)
    if scale:
        left, right = scale.get(str(actual).lower()), scale.get(str(expected).lower())
        if left is None or right is None:
            return None
        return {"lte": left <= right, "gte": left >= right,
                "eq": left == right, "not_eq": left != right}.get(op)

    if isinstance(expected, bool) or isinstance(actual, bool):
        equal = bool(actual) == bool(expected)
        return equal if op in {"eq", "lte", "gte"} else not equal

    left_n, right_n = _numeric(actual), _numeric(expected)
    if left_n is not None and right_n is not None:
        return {"lte": left_n <= right_n, "gte": left_n >= right_n,
                "eq": left_n == right_n, "not_eq": left_n != right_n}.get(op)

    equal = str(actual).strip().lower() == str(expected).strip().lower()
    return equal if op == "eq" else (not equal if op == "not_eq" else None)


def _check_claim(constraint: IntentConstraint, claims: Iterable[ClaimVerification]) -> RequirementMatch:
    wanted = str(constraint.value)
    found = next((c for c in claims if c.claim == wanted), None)
    if found and found.status == "VERIFIED":
        evidence = f"attested by {found.attested_by}"
        if found.certificate:
            evidence += f" (certificate {found.certificate})"
        return RequirementMatch(
            requirement=_label(constraint), source_phrase=constraint.source_phrase,
            satisfied=True, kind=constraint.kind, field="claim",
            actual_value=found.status, evidence=evidence,
        )
    if found and found.status == "ASSERTED_UNATTESTED":
        evidence = ("the merchant asserts this claim but no auditor's certification event "
                    "appears in the product's provenance chain")
    else:
        evidence = "this product makes no such claim"
    return RequirementMatch(
        requirement=_label(constraint), source_phrase=constraint.source_phrase,
        satisfied=False, kind=constraint.kind, field="claim",
        actual_value=(found.status if found else "NOT_CLAIMED"), evidence=evidence,
    )


def assess(
    spec: ProductSpec,
    commercial: dict | None,
    plan: IntentPlan,
    claims: Iterable[ClaimVerification] = (),
    similarity: float = 0.0,
) -> CandidateAssessment:
    """Evaluate one candidate and keep the reasoning."""
    claims = list(claims)
    matched: list[RequirementMatch] = []
    unmet: list[RequirementMatch] = []
    disqualified_by: str | None = None

    for constraint in plan.constraints:
        if constraint.field == "claim":
            result = _check_claim(constraint, claims)
        else:
            actual = _attribute(spec, constraint.field)
            verdict = _satisfies(constraint, actual)
            if verdict is None:
                continue  # field not applicable to this product
            result = RequirementMatch(
                requirement=_label(constraint), source_phrase=constraint.source_phrase,
                satisfied=bool(verdict), kind=constraint.kind, field=constraint.field,
                actual_value=actual,
                evidence=f"{constraint.field.replace('_', ' ')}: {_describe(constraint.field, actual)}",
            )
        (matched if result.satisfied else unmet).append(result)
        if not result.satisfied and constraint.kind == "hard" and disqualified_by is None:
            disqualified_by = result.requirement

    list_price = float(commercial["list_price"]) if commercial and commercial.get("list_price") else None
    if plan.budget and plan.budget_is_hard and list_price and list_price > plan.budget * BUDGET_SLACK:
        disqualified_by = disqualified_by or f"list price ${list_price:,.2f} is beyond a ${plan.budget:,.0f} budget"

    # Soft constraints are the ranking signal. Weighted so that a strong
    # preference ("gets cold easily") outranks a weak one, and normalised so a
    # query with many preferences is not scored on a different scale from one
    # with few.
    weights = {_label(c): c.weight for c in plan.constraints}
    soft_hit = sum(weights.get(m.requirement, 1.0) for m in matched if m.kind == "soft")
    soft_total = sum(c.weight for c in plan.constraints if c.kind == "soft")
    soft_ratio = (soft_hit / soft_total) if soft_total else 1.0
    hard_ok = disqualified_by is None
    fit = (0.55 * soft_ratio + 0.30 * min(max(similarity, 0.0), 1.0) + 0.15) if hard_ok else 0.0

    return CandidateAssessment(
        sku=spec.sku, name=spec.name, similarity=round(similarity, 4),
        fit_score=round(fit, 4), eligible=hard_ok, disqualified_by=disqualified_by,
        matched=matched, unmet=unmet, claims=claims, list_price=list_price,
    )


def rank(assessments: list[CandidateAssessment]) -> list[CandidateAssessment]:
    """Eligible first, then fit, then similarity. Ineligible candidates are
    kept rather than filtered out: the rejected ones and the reason they were
    rejected are what make the winner's case."""
    return sorted(
        assessments,
        key=lambda a: (a.eligible, a.fit_score, a.similarity),
        reverse=True,
    )


def tradeoffs(assessment: CandidateAssessment) -> list[str]:
    """Honest statements of what this product does NOT satisfy.

    Included in the offer deliberately. An agent that is told the downside
    can weigh it; an agent that discovers it later stops trusting the
    merchant's proposals altogether, and in a market where the buyer is a
    machine that distrust is permanent and automated.
    """
    out: list[str] = []
    for miss in assessment.unmet:
        if miss.field == "claim":
            out.append(f"{miss.requirement}: not met -- {miss.evidence}")
        else:
            out.append(f"{miss.requirement}: not met ({miss.evidence})")
    return out
