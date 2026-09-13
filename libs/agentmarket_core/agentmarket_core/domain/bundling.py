"""Dynamic bundling: composing a kit that answers a need, then pricing it.

The brief's example is an agent asking for "beginner-friendly podcasting gear"
and getting back "the ideal bundle" rather than a product. The interesting
part is not discounting a fixed kit -- it is deciding, per request, which
items belong in this particular answer.

Two rules shape the selection:

**Every item has to earn its place by covering something.** An item joins the
kit only if it closes a requirement or use case the current set does not
already cover. That is why the result changes with the query: "gets cold
easily" pulls an insulated mat into the kit, and without that phrase the same
catalog produces a kit without one. A fixed "starter bundle" cannot do this,
and neither can similarity ranking, which would just return five things that
resemble the first one.

**The merchandising decision and the pricing decision are separated.**
Choosing the SKUs needs no commercial data, so the storefront does it from
public fields. Pricing the kit needs cost and MAP for every component, so it
happens inside the pricing service. The discount comes only out of the
headroom above each component's own floor -- a bundle can never be the route
by which a product is sold below the floor it would have had on its own.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable

from agentmarket_core.config import settings
from agentmarket_core.models import (
    Bundle,
    BundleItem,
    CandidateAssessment,
    IntentPlan,
    PriceQuote,
    ProductSpec,
)

log = logging.getLogger("agentmarket.bundling")

# Budget headroom used while selecting, before real quotes exist. List price
# is an upper bound on what the pricing engine will ask in the normal case, so
# selecting against it is conservative; the hard check runs on the real total.
SELECTION_BUDGET_SLACK = 1.02


def _uses(spec: ProductSpec) -> set[str]:
    return {str(u) for u in (spec.attributes or {}).get("use_cases", [])}


def _product_type(spec: ProductSpec) -> str:
    return str((spec.attributes or {}).get("product_type") or spec.category)


def select_items(
    primary: CandidateAssessment,
    candidates: Iterable[tuple[CandidateAssessment, ProductSpec]],
    plan: IntentPlan,
    primary_spec: ProductSpec,
    complements: Iterable[str] = (),
    max_items: int | None = None,
) -> tuple[list[str], list[dict]]:
    """Choose the SKUs in the kit. Returns (skus, dropped).

    Public fields only: nothing here reads cost or MAP.
    """
    max_items = max_items or settings.bundle_max_items
    complement_set = {str(s) for s in complements}

    chosen = [primary.sku]
    dropped: list[dict] = []
    covered_types = {_product_type(primary_spec)}
    wanted_uses = set(plan.use_cases)
    running_total = primary.list_price or 0.0
    budget = plan.budget

    def priority(pair: tuple[CandidateAssessment, ProductSpec]) -> tuple:
        assessment, spec = pair
        covers_use = bool(_uses(spec) & wanted_uses)
        return (assessment.sku in complement_set, covers_use, assessment.fit_score)

    for assessment, spec in sorted(candidates, key=priority, reverse=True):
        if assessment.sku in chosen:
            continue
        if len(chosen) >= max_items:
            break

        # Ineligible means it failed a requirement the buyer stated. It does
        # not go in the kit, and it is recorded so the offer can say why --
        # a kit that silently omits the ethical option is indistinguishable
        # from a kit that never found one.
        if not assessment.eligible:
            dropped.append({"sku": assessment.sku, "name": assessment.name,
                            "reason": assessment.disqualified_by or "did not meet a stated requirement"})
            continue

        # One of each kind of thing. Two sleeping bags is not a kit, and
        # product type is the right grain for that test: a sleeping bag and a
        # sleeping mat are both "gear" and are not substitutes for each other.
        item_type = _product_type(spec)
        if item_type in covered_types:
            continue

        estimated = running_total + (assessment.list_price or 0.0)
        if budget and estimated > budget * SELECTION_BUDGET_SLACK:
            dropped.append({"sku": assessment.sku, "name": assessment.name,
                            "reason": f"would take the kit past the ${budget:,.0f} budget"})
            continue

        chosen.append(assessment.sku)
        running_total = estimated
        covered_types.add(item_type)

    return chosen, dropped


# What each kind of item is actually for. Written as the reason a person
# would give, because the justification has to survive being read aloud by
# the buyer's agent to the buyer.
TYPE_ROLES = {
    "sleeping_bag": "somewhere warm to sleep, which is the part a first-timer most often gets wrong",
    "sleeping_mat": "insulation from the ground, where most of the cold at night actually comes from",
    "daypack": "something to carry the rest of it in",
    "backpack": "something to carry the rest of it in",
    "water_filter": "drinkable water without carrying all of it",
    "headlamp": "light for the end of the day",
    "trekking_poles": "takes load off the knees on descents",
    "first_aid_kit": "blisters and small injuries, which is what actually ends first hikes",
    "insulated_bottle": "a hot drink on a cold morning",
    "hydration_bladder": "drinking without stopping to unpack",
    "rain_jacket": "the wet-weather layer",
}


def role_in_bundle(spec: ProductSpec, plan: IntentPlan, primary_sku: str) -> str:
    """One phrase saying what this item is doing in this kit."""
    if spec.sku == primary_sku:
        return "the core item this request is about"
    uses = _uses(spec) & set(plan.use_cases)
    if "cold_weather" in uses:
        return "covers the stated sensitivity to cold"
    if "wet_weather" in uses:
        return "covers the wet-weather requirement"
    role = TYPE_ROLES.get(_product_type(spec))
    if role:
        return role
    if uses:
        return "covers " + ", ".join(sorted(u.replace("_", " ") for u in uses))
    return "completes the kit for first use"


# ---------------------------------------------------------------------------
# Pricing half -- runs inside the pricing service, where cost and MAP live.
# ---------------------------------------------------------------------------

def price_bundle(
    skus: list[str],
    quote_fn: Callable[[str], PriceQuote],
    floor_fn: Callable[[str], float],
) -> tuple[list[PriceQuote], float, float, float, list[str]]:
    """Quote every component and compute the bundle discount.

    Returns (quotes, subtotal, discount, total, guardrails).

    The discount is bounded twice: by the configured maximum ratio, and by the
    actual headroom between each component's quote and its own floor. The
    second bound is the one that matters -- it makes "sold below MAP as part of
    a bundle" structurally impossible rather than a thing we remember not to
    do.
    """
    quotes = [quote_fn(sku) for sku in skus]
    subtotal = round(sum(q.amount for q in quotes), 2)
    headroom = round(sum(max(0.0, q.amount - floor_fn(q.sku)) for q in quotes), 2)

    guardrails: list[str] = []
    target = round(subtotal * settings.bundle_max_discount_ratio, 2)
    discount = target
    if discount > headroom:
        discount = headroom
        guardrails.append("BUNDLE_FLOOR")
    else:
        guardrails.append("BUNDLE_DISCOUNT_CAP")
    discount = max(0.0, round(discount, 2))
    total = round(subtotal - discount, 2)
    return quotes, subtotal, discount, total, guardrails


def build_bundle(
    quotes: list[PriceQuote],
    specs: dict[str, ProductSpec],
    plan: IntentPlan,
    primary_sku: str,
    trust: dict[str, tuple[str, str]],
    subtotal: float,
    discount: float,
    total: float,
    guardrails: list[str],
    dropped: list[dict] | None = None,
) -> Bundle:
    items: list[BundleItem] = []
    for quote in quotes:
        spec = specs[quote.sku]
        token_ref, status = trust.get(quote.sku, (quote.sku, "FAIL"))
        items.append(BundleItem(
            sku=quote.sku,
            name=spec.name,
            role="core" if quote.sku == primary_sku else "accessory",
            role_in_bundle=role_in_bundle(spec, plan, primary_sku),
            price=quote,
            trust_token_ref=token_ref,
            trust_status=status,  # type: ignore[arg-type]
            attributes=dict(spec.attributes or {}),
            essential=quote.sku == primary_sku,
        ))
    return Bundle(
        items=items, subtotal=subtotal, bundle_discount=discount, total=total,
        guardrails_applied=guardrails, dropped=list(dropped or []),
    )
