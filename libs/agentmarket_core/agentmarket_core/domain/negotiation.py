"""Agent-to-agent negotiation.

The brief describes it directly: "an API-driven system that lets the retailer's
AI negotiate with the buyer's AI. If the buyer's agent is optimising against a
strict budget, the merchant's system might automatically offer a personalised
discount or propose a slightly older model that fits the constraint exactly."

Which means "no" is the least useful answer a merchant can give. A buyer's
agent that is $40 over budget is not a lost sale, it is an unanswered
question: can you move, is there something else, can the kit be smaller. This
module answers it in four ways -- concede, concede partially, propose a
different product, restructure the kit -- and holds only when none of them is
possible.

Two things it will not do.

**It will not leak the merchant's economics.** The concession decision is
computed from cost and MAP inside the pricing service; what crosses the wire
is a price and a reason code. `FLOOR_REACHED` tells the buyer's agent to stop
pushing without telling it what the margin was, which is exactly the amount of
information a human sales rep would give.

**It will not discount for the sake of closing.** Two bounds apply: the
per-item floor (cost plus minimum margin, and MAP), and a cap on how far any
single negotiation may travel from the opening ask. Without the second bound
the optimal strategy for every buyer's agent is to counter at $1, and an
automated counterparty will find that out immediately and permanently.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass

from agentmarket_core import db
from agentmarket_core.config import settings
from agentmarket_core.models import NegotiationRound

log = logging.getLogger("agentmarket.negotiation")

REASON_FLOOR_REACHED = "FLOOR_REACHED"
REASON_CONCESSION_LIMIT = "CONCESSION_LIMIT_REACHED"
REASON_ROUNDS_EXHAUSTED = "ROUNDS_EXHAUSTED"
REASON_NO_ALTERNATIVE = "NO_ALTERNATIVE_WITHIN_CONSTRAINTS"


@dataclass(frozen=True)
class ConcessionDecision:
    """What the pricing engine is willing to do, with nothing confidential in it."""

    outcome: str                   # CONCEDED | PARTIAL_CONCESSION | HELD
    amount: float                  # the best price available on this SKU
    reason_code: str | None
    moved_from: float

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "amount": round(self.amount, 2),
            "reason_code": self.reason_code,
            "moved_from": round(self.moved_from, 2),
        }


def evaluate_concession(
    opening_amount: float,
    target_amount: float,
    floor: float,
    fair_value: float,
) -> ConcessionDecision:
    """Decide how far the merchant can move on one SKU.

    Pure, so it is testable without a database and identical in every caller.
    `floor` is already the maximum of MAP and cost-plus-minimum-margin; this
    function never sees the components, which is what keeps the caller honest
    about not returning them.
    """
    opening_amount = round(float(opening_amount), 2)
    target_amount = round(float(target_amount), 2)

    # A counter at or above the standing offer is an acceptance in all but
    # name. Charging more than the buyer's agent asked for would be a strange
    # way to win the negotiation.
    if target_amount >= opening_amount:
        return ConcessionDecision("CONCEDED", opening_amount, None, opening_amount)

    concession_limit = round(opening_amount - fair_value * settings.max_concession_ratio, 2)
    best = max(round(floor, 2), concession_limit)

    if target_amount >= best:
        return ConcessionDecision("CONCEDED", target_amount, None, opening_amount)
    if best < opening_amount:
        reason = REASON_FLOOR_REACHED if best <= round(floor, 2) + 0.001 else REASON_CONCESSION_LIMIT
        return ConcessionDecision("PARTIAL_CONCESSION", best, reason, opening_amount)
    return ConcessionDecision("HELD", opening_amount, REASON_FLOOR_REACHED, opening_amount)


def item_floor(internal_cost: float, map_price: float | None) -> float:
    """The price below which a component may not be sold, bundled or not."""
    margin_floor = float(internal_cost) * (1 + settings.min_margin_ratio)
    return round(max(margin_floor, float(map_price or 0.0)), 2)


# ---------------------------------------------------------------------------
# Persistence. A negotiation outlives the request that opened it.
# ---------------------------------------------------------------------------

class NegotiationStore:
    def __init__(self, persist: bool = True) -> None:
        self.persist = persist
        self._memory: dict[str, dict] = {}

    @staticmethod
    def new_id() -> str:
        return f"neg_{uuid.uuid4().hex[:12]}"

    def create(self, agent_id: str, sku: str, amount: float, quote_id: str | None,
               context: dict | None = None) -> dict:
        record = {
            "negotiation_id": self.new_id(),
            "agent_id": agent_id,
            "sku": sku,
            "opening_amount": round(amount, 2),
            "current_amount": round(amount, 2),
            "current_quote": quote_id,
            "status": "OPEN",
            "rounds": [],
            "context": context or {},
            "opened_at": time.time(),
        }
        if not self.persist:
            self._memory[record["negotiation_id"]] = record
            return record
        db.execute(
            """INSERT INTO negotiations (negotiation_id, agent_id, sku, opening_amount,
                                         current_amount, current_quote, status, rounds, context)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
            (record["negotiation_id"], agent_id, sku, record["opening_amount"],
             record["current_amount"], quote_id, "OPEN", json.dumps([]),
             json.dumps(record["context"])),
        )
        return record

    def get(self, negotiation_id: str) -> dict | None:
        if not self.persist:
            return self._memory.get(negotiation_id)
        row = db.query_one(
            """SELECT negotiation_id, agent_id, sku,
                      opening_amount::float AS opening_amount,
                      current_amount::float AS current_amount,
                      current_quote, status, rounds, context,
                      extract(epoch FROM opened_at) AS opened_at
                 FROM negotiations WHERE negotiation_id = %s""",
            (negotiation_id,),
        )
        return dict(row) if row else None

    def append_round(self, negotiation_id: str, rounds: list[NegotiationRound],
                     status: str, amount: float, quote_id: str | None,
                     sku: str | None = None, context: dict | None = None) -> None:
        """Record a round and, when the offer changed shape, the new context.

        Persisting the context matters as much as the amount. A restructured
        kit that leaves `context.bundle_skus` pointing at the old composition
        makes the next round negotiate against a bundle that is no longer on
        the table -- which shows up as the merchant's price moving *upward*
        between rounds, a bug that only appears on the third message and
        destroys the buyer agent's trust immediately.
        """
        payload = [r.model_dump() for r in rounds]
        if not self.persist:
            record = self._memory.get(negotiation_id)
            if record:
                record.update(rounds=payload, status=status, current_amount=round(amount, 2),
                              current_quote=quote_id, sku=sku or record["sku"])
                if context is not None:
                    record["context"] = context
            return
        db.execute(
            """UPDATE negotiations
                  SET rounds = %s::jsonb, status = %s, current_amount = %s,
                      current_quote = %s, sku = COALESCE(%s, sku),
                      context = COALESCE(%s::jsonb, context), updated_at = now()
                WHERE negotiation_id = %s""",
            (json.dumps(payload), status, round(amount, 2), quote_id, sku,
             json.dumps(context) if context is not None else None, negotiation_id),
        )

    def recent(self, limit: int = 25) -> list[dict]:
        if not self.persist:
            return list(self._memory.values())[:limit]
        return db.query(
            """SELECT negotiation_id, agent_id, sku,
                      opening_amount::float AS opening_amount,
                      current_amount::float AS current_amount,
                      status, rounds,
                      extract(epoch FROM opened_at) AS opened_at
                 FROM negotiations ORDER BY opened_at DESC LIMIT %s""",
            (limit,),
        )


def rounds_from(raw: list[dict] | None) -> list[NegotiationRound]:
    out: list[NegotiationRound] = []
    for item in raw or []:
        try:
            out.append(NegotiationRound(**item))
        except (TypeError, ValueError):
            continue
    return out


def buyer_round(index: int, amount: float, reason: str = "") -> NegotiationRound:
    return NegotiationRound(
        round=index, actor="buyer_agent", proposed_amount=round(amount, 2),
        message=reason or f"buyer agent counters at ${amount:,.2f}",
    )


def merchant_round(index: int, outcome: str, amount: float | None, message: str,
                   reason_code: str | None = None, quote_id: str | None = None,
                   sku: str | None = None) -> NegotiationRound:
    return NegotiationRound(
        round=index, actor="merchant_agent", proposed_amount=amount,
        outcome=outcome,  # type: ignore[arg-type]
        reason_code=reason_code, message=message, quote_id=quote_id, sku=sku,
    )
