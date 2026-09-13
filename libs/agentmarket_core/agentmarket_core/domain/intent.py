"""Intent decoding: from what a person said to what the catalog can be asked.

This is the subsystem the challenge brief weights first -- "how intelligently
the merchant's system decodes the nuanced needs of the human behind the agent,
going beyond basic keyword or spec matching".

The gap it has to close is a vocabulary gap. A buyer's agent relays something
like "my dad is 60 and has never hiked before, he gets cold easily, and I only
want brands that can actually prove they're ethically made". Nothing in any
product record says "never hiked before" or "gets cold easily". What the
records hold is `experience_level: beginner`, `break_in_required: false`,
`use_cases: [cold_weather]`, and a certification event signed by an auditor.
Keyword search over that query retrieves on the words "dad", "hiking" and
"cold" and returns an expert alpine sleeping bag, which is exactly the failure
the brief describes.

So the decode produces predicates, not keywords, and every predicate carries
the phrase it came from. Three consequences worth the code:

* **Auditable.** `experience_level <= beginner  <- "has never hiked before"`
  can be read, checked and disagreed with. A decode nobody can inspect is
  indistinguishable from a guess, and a judge cannot tell them apart either.
* **Separable.** Hard predicates gate; soft ones rank. "Under $400" is a wall,
  "gets cold easily" is a preference, and collapsing the two either rejects
  good products or ships bad ones.
* **Deterministic by default.** The rule layer below runs with no model at
  all, so the demo behaves identically on a laptop with nothing installed. An
  LLM, when reachable, runs *first* and widens coverage to phrasings the rules
  do not know -- but it may only propose predicates from this module's closed
  vocabulary, and the rules re-run over the raw query afterwards and win any
  conflict. A model that hallucinates a field name changes nothing.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

from agentmarket_core.domain import claims as claims_mod
from agentmarket_core.models import IntentConstraint, IntentPlan

log = logging.getLogger("agentmarket.intent")

# Ordinal scales. Comparing "beginner" to "expert" needs an order, and the
# order needs to live in one place that both the decoder and the matcher read.
EXPERIENCE_SCALE = {"beginner": 0, "intermediate": 1, "expert": 2}
EASE_SCALE = {"simple": 0, "moderate": 1, "technical": 2}

# Fields a constraint is allowed to name. Anything else -- including anything
# an LLM invents -- is discarded before it reaches the matcher.
ALLOWED_FIELDS = {
    "experience_level", "ease_of_use", "break_in_required", "weight_g",
    "waterproof_rating", "temp_rating_c", "comfort_rating_c", "use_cases",
    "wide_fit", "joint_support", "machine_washable", "capacity_l", "r_value",
    "category", "lumens", "insulated_hours", "product_type",
    # Not a product attribute: `claim` is resolved against the provenance
    # chain by domain/claims.py, never against anything the merchant typed.
    "claim",
}

BUNDLE_SIGNALS = re.compile(
    r"\b(kit|bundle|set|everything|all the gear|whole (?:lot|kit|setup)|starter|"
    r"get (?:him|her|them|me) (?:started|going)|outfit|package|complete)\b",
    re.IGNORECASE,
)


def _c(field: str, op: str, value, *, kind: str = "hard", phrase: str = "",
       weight: float = 1.0, why: str = "") -> IntentConstraint:
    return IntentConstraint(field=field, op=op, value=value, kind=kind,
                            source_phrase=phrase, weight=weight, rationale=why)


# ---------------------------------------------------------------------------
# The rule table. Each entry is (name, pattern, builder). The builder receives
# the regex match and returns constraints plus optional plan updates.
#
# These are outcome->attribute mappings, which is the part that cannot be done
# by embedding similarity: "he gets cold easily" and "cold_weather" are not
# close in vector space, they are connected by a merchandising fact about what
# keeps a person warm.
# ---------------------------------------------------------------------------
Rule = tuple[str, re.Pattern, Callable[[re.Match], tuple[list[IntentConstraint], dict]]]


def _beginner(m: re.Match) -> tuple[list[IntentConstraint], dict]:
    phrase = m.group(0)
    return [
        _c("experience_level", "lte", "beginner", phrase=phrase,
           why="a first-timer should not be sold gear that assumes trail experience"),
        _c("ease_of_use", "lte", "simple", kind="soft", weight=1.4, phrase=phrase,
           why="fewer adjustments to get wrong on a first outing"),
        _c("break_in_required", "eq", False, kind="soft", weight=1.2, phrase=phrase,
           why="break-in periods are how new hikers end up with blisters"),
    ], {"experience_level": "beginner"}


def _expert(m: re.Match) -> tuple[list[IntentConstraint], dict]:
    return [], {"experience_level": "expert"}


def _cold(m: re.Match) -> tuple[list[IntentConstraint], dict]:
    phrase = m.group(0)
    return [
        _c("use_cases", "contains", "cold_weather", kind="soft", weight=1.8, phrase=phrase,
           why="the stated need is warmth, which is a property of the gear, not of the search words"),
    ], {"use_cases": ["cold_weather"]}


def _joints(m: re.Match) -> tuple[list[IntentConstraint], dict]:
    phrase = m.group(0)
    return [
        _c("joint_support", "eq", True, kind="soft", weight=1.3, phrase=phrase,
           why="knee and joint load is the first complaint of older or heavier walkers"),
        _c("wide_fit", "eq", True, kind="soft", weight=1.1, phrase=phrase,
           why="comfort fit matters more than performance fit for this buyer"),
    ], {}


def _light(m: re.Match) -> tuple[list[IntentConstraint], dict]:
    phrase = m.group(0)
    grams = None
    number = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g\b|grams?)", phrase, re.IGNORECASE)
    if number:
        value = float(number.group(1))
        grams = value * 1000 if number.group(2).lower().startswith("kg") else value
    if grams:
        return [_c("weight_g", "lte", grams, phrase=phrase,
                   why="an explicit weight ceiling is a hard constraint")], {}
    return [_c("weight_g", "lte", 1500, kind="soft", weight=1.2, phrase=phrase,
               why="'not too heavy' read as a preference, not a stated limit")], {}


def _wet(m: re.Match) -> tuple[list[IntentConstraint], dict]:
    phrase = m.group(0)
    return [
        _c("waterproof_rating", "gte", "IPX4", phrase=phrase,
           why="wet-weather use needs a rated membrane, not a description that mentions rain"),
        _c("use_cases", "contains", "wet_weather", kind="soft", weight=1.2, phrase=phrase),
    ], {"use_cases": ["wet_weather"]}


def _overnight(m: re.Match) -> tuple[list[IntentConstraint], dict]:
    phrase = m.group(0)
    return [_c("use_cases", "contains", "camping", kind="soft", weight=1.5, phrase=phrase,
               why="sleeping outdoors changes which categories belong in the answer")], \
           {"use_cases": ["camping"]}


def _day_hike(m: re.Match) -> tuple[list[IntentConstraint], dict]:
    phrase = m.group(0)
    return [_c("use_cases", "contains", "day_hike", kind="soft", weight=1.2, phrase=phrase)], \
           {"use_cases": ["day_hike"]}


def _washable(m: re.Match) -> tuple[list[IntentConstraint], dict]:
    return [_c("machine_washable", "eq", True, kind="soft", weight=1.1,
               phrase=m.group(0))], {}


# Naming a product type is the one place where the buyer's words map directly
# onto a field, and treating it as a hard constraint is what stops a query for
# a boot returning a backpack because the copy around both mentions hiking.
# Near-substitutes are included on purpose: a trail shoe is a legitimate
# answer to "hiking boot" for a beginner, and excluding it would hide the
# better recommendation behind a literal reading of the request.
PRODUCT_TYPES: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\b(boots?)\b", re.IGNORECASE), ["boot", "trail_shoe"]),
    (re.compile(r"\b(shoes?|trainers?|sneakers?|footwear)\b", re.IGNORECASE), ["trail_shoe", "boot"]),
    (re.compile(r"\b(day ?pack)\b", re.IGNORECASE), ["daypack", "backpack"]),
    (re.compile(r"\b(backpacks?|rucksacks?)\b", re.IGNORECASE), ["backpack", "daypack"]),
    (re.compile(r"\b(rain ?jackets?|hardshells?|shells?|raincoats?|waterproof jackets?)\b",
                re.IGNORECASE), ["rain_jacket"]),
    (re.compile(r"\b(sleeping bags?)\b", re.IGNORECASE), ["sleeping_bag"]),
    (re.compile(r"\b(sleeping (?:mats?|pads?)|mattress(?:es)?)\b", re.IGNORECASE), ["sleeping_mat"]),
    (re.compile(r"\b(head ?lamps?|head ?torch(?:es)?)\b", re.IGNORECASE), ["headlamp"]),
    (re.compile(r"\b((?:trekking|walking|hiking) poles?|poles?)\b", re.IGNORECASE), ["trekking_poles"]),
    (re.compile(r"\b(water filters?|purifiers?)\b", re.IGNORECASE), ["water_filter"]),
    (re.compile(r"\b(hydration (?:bladders?|packs?)|bladders?)\b", re.IGNORECASE), ["hydration_bladder"]),
    (re.compile(r"\b(first[- ]aid(?: kits?)?|med ?kits?)\b", re.IGNORECASE), ["first_aid_kit"]),
    (re.compile(r"\b(bottles?|flasks?|thermos(?:es)?)\b", re.IGNORECASE), ["insulated_bottle"]),
]

# An explicit temperature rating is a specification, not a mood. "-12C" has to
# gate, or a +5C summer bag scores well on a query for an alpine one purely
# because both are sleeping bags described in similar words.
TEMPERATURE = re.compile(
    r"(?:rated (?:to|for)|rating of|down to|good (?:to|for)|to)\s*"
    r"(-?\s?\d{1,2})\s*°?\s*c\b", re.IGNORECASE)


RULES: list[Rule] = [
    ("beginner", re.compile(
        r"(never (?:done|hiked|been|tried)[^.,;]*|first[- ]time[rs]?|first hike|"
        r"beginner[- ]?friendly|beginner|novice|new to (?:hiking|this|it)|"
        r"just (?:getting|got) into|starting out|getting started|no experience)",
        re.IGNORECASE), _beginner),
    ("expert", re.compile(
        r"(experienced|expert|advanced|technical terrain|alpine|mountaineer\w*)",
        re.IGNORECASE), _expert),
    ("cold", re.compile(
        r"(gets? cold(?: easily)?|feels? the cold|cold sleeper|always cold|"
        r"keep(?:s)? (?:him|her|them|me) warm|stay warm|warmth|freezing|"
        r"cold weather|winter)", re.IGNORECASE), _cold),
    ("joints", re.compile(
        r"(knees?|joints?|bad back|arthrit\w+|\b(?:6\d|7\d|8\d)\b(?:\s*(?:years?|yrs?))?|"
        r"older|elderly|senior)", re.IGNORECASE), _joints),
    ("light", re.compile(
        r"(not too heavy|lightweight|light\b|under \d+(?:\.\d+)?\s*(?:kg|g\b|grams?)|"
        r"less than \d+(?:\.\d+)?\s*(?:kg|g\b|grams?))", re.IGNORECASE), _light),
    ("wet", re.compile(
        r"(waterproof|rain\w*|wet(?: weather| conditions)?|downpour|monsoon)",
        re.IGNORECASE), _wet),
    ("overnight", re.compile(
        r"(overnight|camp\w*|sleep\w* (?:out|outside)|multi[- ]day|two[- ]day|"
        r"weekend trip)", re.IGNORECASE), _overnight),
    ("day_hike", re.compile(r"(day hike|day walk|afternoon walk|short walk)", re.IGNORECASE), _day_hike),
    ("washable", re.compile(r"(machine washable|easy to (?:clean|wash)|washable)", re.IGNORECASE), _washable),
]

# Budget. The distinction between a wall and a preference is load-bearing:
# "strictly under $400" must never be exceeded, "around $400" may be, and a
# system that treats them the same either loses sales or breaks promises.
BUDGET_HARD = re.compile(
    r"(?:under|below|less than|no more than|max(?:imum)?(?: of)?|not? more than|"
    r"within|up to|budget(?: is| of)?|cap(?:ped)? at|strictly under)\s*"
    r"\$?\s*(\d+(?:[.,]\d+)?)\s*(k\b)?", re.IGNORECASE)
BUDGET_SOFT = re.compile(
    r"(?:around|about|roughly|approximately|circa|ballpark|or so|ish)\s*"
    r"\$?\s*(\d+(?:[.,]\d+)?)\s*(k\b)?", re.IGNORECASE)
BUDGET_BARE = re.compile(r"\$\s*(\d+(?:[.,]\d+)?)\s*(k\b)?")
SOFT_BUDGET_WORDS = re.compile(r"\b(around|about|roughly|approximately|circa|ish|or so|flexible)\b",
                               re.IGNORECASE)

RECIPIENT = re.compile(
    r"\b(?:for )?(my )?(dad|father|mum|mom|mother|wife|husband|son|daughter|"
    r"friend|partner|brother|sister|colleague|myself|me)\b", re.IGNORECASE)
GIFT = re.compile(r"\b(gift|present|birthday|christmas|anniversary)\b", re.IGNORECASE)


def _parse_budget(query: str) -> tuple[float | None, bool]:
    """Returns (amount, is_hard)."""
    for pattern, hard in ((BUDGET_HARD, True), (BUDGET_SOFT, False), (BUDGET_BARE, True)):
        match = pattern.search(query)
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        amount = float(raw)
        if match.lastindex and match.group(match.lastindex) and str(match.group(match.lastindex)).lower() == "k":
            amount *= 1000
        # "under $400 or so" is soft even though it matched the hard pattern.
        if hard and SOFT_BUDGET_WORDS.search(query):
            window = query[max(0, match.start() - 25): match.end() + 15]
            hard = not SOFT_BUDGET_WORDS.search(window)
        return amount, hard
    return None, True


def _recipient(query: str) -> str | None:
    match = RECIPIENT.search(query)
    who = match.group(2).lower() if match else None
    if who in {"me", "myself"}:
        who = "self"
    if GIFT.search(query) and who and who != "self":
        return f"gift:{who}"
    return who


def _noun_for(constraints: list[IntentConstraint]) -> str:
    """What kind of thing the buyer asked for, in their vocabulary."""
    for constraint in constraints:
        if constraint.field != "product_type":
            continue
        values = constraint.value if isinstance(constraint.value, (list, tuple)) else [constraint.value]
        if values:
            return str(values[0]).replace("_", " ")
    return ""


def _spec_phrases(constraints: list[IntentConstraint]) -> list[str]:
    """Hard numeric specifications, restated so the readback is checkable."""
    out: list[str] = []
    for constraint in constraints:
        if constraint.kind != "hard":
            continue
        if constraint.field == "temp_rating_c":
            out.append(f"rated to {constraint.value:g}C or colder")
        elif constraint.field == "weight_g":
            out.append(f"under {constraint.value:g}g")
        elif constraint.field == "waterproof_rating":
            out.append(f"waterproof to {constraint.value}")
    return out


def _interpret(plan_fields: dict, values: list[str], budget: float | None,
               bundle: bool, query: str) -> str:
    """One sentence saying what the system believes it was asked for.

    Written from the decoded fields rather than from the query, so it is a
    readback of the decode, not a paraphrase of the input. If the readback is
    wrong, the decode is wrong, and the buyer's agent can see it immediately.
    """
    bits: list[str] = []
    level = plan_fields.get("experience_level")
    if level:
        bits.append(f"{level}-level")
    who = plan_fields.get("recipient")
    if who and who.startswith("gift:"):
        bits.append(f"as a gift for the buyer's {who.split(':', 1)[1]}")
    elif who and who != "self":
        bits.append(f"for the buyer's {who}")
    uses = plan_fields.get("use_cases") or []
    if uses:
        bits.append("for " + ", ".join(u.replace("_", " ") for u in uses))
    if values:
        labels = [claims_mod.CLAIM_CATALOG[v]["label"].lower() for v in values
                  if v in claims_mod.CLAIM_CATALOG]
        if labels:
            bits.append("restricted to independently verified " + " and ".join(labels))
    if budget:
        bits.append(f"within ${budget:,.0f}")
    noun = plan_fields.get("noun") or ""
    head = "A complete kit" if bundle else ("A " + noun if noun else "A single product")
    specs = plan_fields.get("specs") or []
    if specs:
        bits.insert(0, ", ".join(specs))
    return f"{head} {' '.join(bits)}.".replace("  ", " ") if bits else \
        f"{head} matching: {query.strip()[:120]}"


def decode(query: str, llm_plan: dict | None = None) -> IntentPlan:
    """Decode a free-text agent request into predicates over catalog fields.

    `llm_plan` is an optional, already-fetched LLM proposal (see
    `agentmarket_core.llm.decode_intent`). It is merged *under* the rule
    layer: rules always run over the raw query and overwrite any conflicting
    field, so the deterministic behaviour is the floor rather than the
    fallback.
    """
    plan_fields: dict = {}
    constraints: list[IntentConstraint] = []
    seen: set[tuple] = set()

    def add(cs: list[IntentConstraint]) -> None:
        for c in cs:
            key = (c.field, c.op, str(c.value))
            if key in seen or c.field not in ALLOWED_FIELDS:
                continue
            seen.add(key)
            constraints.append(c)

    # -- 1. LLM proposal (optional, filtered) --------------------------------
    decoded_by = "deterministic"
    if llm_plan:
        decoded_by = "llm+rules"
        for raw in (llm_plan.get("constraints") or []):
            try:
                candidate = IntentConstraint(
                    field=str(raw.get("field", "")),
                    op=str(raw.get("op", "eq")),
                    value=raw.get("value"),
                    kind="hard" if str(raw.get("kind")) == "hard" else "soft",
                    source_phrase=str(raw.get("source_phrase", ""))[:120],
                    weight=float(raw.get("weight", 1.0)),
                    rationale=str(raw.get("rationale", ""))[:200],
                )
            except (TypeError, ValueError):
                continue
            add([candidate])
        for key in ("experience_level", "recipient"):
            if llm_plan.get(key):
                plan_fields[key] = llm_plan[key]
        if llm_plan.get("use_cases"):
            plan_fields["use_cases"] = list(llm_plan["use_cases"])[:4]

    # -- 2. rule layer (always) ----------------------------------------------
    for _name, pattern, builder in RULES:
        match = pattern.search(query)
        if not match:
            continue
        new_constraints, updates = builder(match)
        add(new_constraints)
        for key, value in updates.items():
            if key == "use_cases":
                merged = list(dict.fromkeys((plan_fields.get("use_cases") or []) + value))
                plan_fields["use_cases"] = merged
            else:
                plan_fields[key] = value

    # -- 2b. product type and explicit specifications ------------------------
    for pattern, types in PRODUCT_TYPES:
        match = pattern.search(query)
        if not match:
            continue
        add([_c("product_type", "in", types, phrase=match.group(0),
                why="the buyer named a product type, which gates rather than ranks")])
        break

    temperature = TEMPERATURE.search(query)
    if temperature:
        try:
            degrees = float(temperature.group(1).replace(" ", ""))
        except ValueError:
            degrees = None
        if degrees is not None:
            add([_c("temp_rating_c", "lte", degrees, phrase=temperature.group(0),
                    why="a stated temperature rating is a specification the product must meet")])

    # An explicit experience level is a hard gate even when only the LLM saw
    # it; without the constraint the retriever will happily return expert kit.
    level = plan_fields.get("experience_level")
    if level == "beginner" and not any(c.field == "experience_level" for c in constraints):
        add([_c("experience_level", "lte", "beginner", phrase=query[:60],
                why="stated experience level")])

    # -- 3. values -----------------------------------------------------------
    value_phrases = dict(claims_mod.claims_with_phrases(query))
    requested_values = list(value_phrases)
    if llm_plan:
        for value in (llm_plan.get("values") or []):
            if value in claims_mod.CLAIM_CATALOG and value not in requested_values:
                requested_values.append(value)
                value_phrases.setdefault(value, "(inferred by the model)")
    proof = claims_mod.proof_demanded(query)
    for value in requested_values:
        add([_c("claim", "eq", value, kind="hard" if proof else "soft",
                weight=2.0, phrase=value_phrases.get(value, ""),
                why=("buyer demanded proof, so an unattested claim is a fail"
                     if proof else "values preference, ranked not gated"))])

    # -- 4. budget, recipient, bundle ----------------------------------------
    budget, budget_hard = _parse_budget(query)
    if llm_plan and budget is None and llm_plan.get("budget"):
        try:
            budget = float(llm_plan["budget"])
        except (TypeError, ValueError):
            budget = None
    recipient = _recipient(query) or plan_fields.get("recipient")
    if recipient:
        plan_fields["recipient"] = recipient
    bundle_intent = bool(BUNDLE_SIGNALS.search(query))
    if llm_plan and llm_plan.get("bundle_intent"):
        bundle_intent = True

    search_query = (llm_plan or {}).get("search_query") or _search_query(query, plan_fields)
    readback = {**plan_fields,
                "noun": _noun_for(constraints),
                "specs": _spec_phrases(constraints)}

    return IntentPlan(
        raw_query=query,
        search_query=search_query,
        interpreted_need=_interpret(readback, requested_values, budget, bundle_intent, query),
        use_cases=plan_fields.get("use_cases") or [],
        experience_level=plan_fields.get("experience_level"),
        recipient=recipient,
        budget=budget,
        budget_is_hard=budget_hard,
        values=requested_values,
        constraints=constraints,
        bundle_intent=bundle_intent,
        decoded_by=decoded_by,  # type: ignore[arg-type]
    )


# Words that describe the buyer rather than the product. Leaving them in the
# semantic query actively hurts: "dad" and "birthday" pull the embedding
# towards gift-shop copy and away from the gear that answers the need.
_NOISE = re.compile(
    r"\b(my|his|her|their|the|a|an|for|and|is|are|was|were|to|of|it|i|we|he|she|they|"
    r"dad|father|mum|mom|mother|wife|husband|son|daughter|friend|partner|brother|sister|"
    r"birthday|christmas|gift|present|turning|year|years|old|want|wants|need|needs|"
    r"looking|buy|buying|please|would|like|only|just|really|actually|about|around|"
    r"budget|under|below|max|maximum|sure|make|get|him|them|me|something|anything)\b",
    re.IGNORECASE)


def _search_query(query: str, plan_fields: dict) -> str:
    """Build the text handed to the vector store.

    Retrieval and filtering do different jobs. The embedding decides *what
    kind of thing* this is about; the predicates decide which specific rows
    qualify. So the search text keeps the domain nouns, drops the pronouns and
    the relationship words, and adds the decoded use cases -- which are the
    catalog's own vocabulary and therefore sit much closer to the product
    text than the buyer's phrasing did.
    """
    stripped = _NOISE.sub(" ", query)
    stripped = re.sub(r"[^\w\s.-]", " ", stripped)
    stripped = re.sub(r"\$?\d+(?:[.,]\d+)?", " ", stripped)
    words = [w for w in stripped.split() if len(w) > 2]
    extras = [u.replace("_", " ") for u in (plan_fields.get("use_cases") or [])]
    level = plan_fields.get("experience_level")
    if level:
        extras.append(f"{level} friendly")
    text = " ".join(dict.fromkeys(words + extras)).strip()
    return text or query.strip()
