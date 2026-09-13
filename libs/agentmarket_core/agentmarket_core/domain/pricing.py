"""Algorithmic Pricing Engine (proposal §4.3):

    1. Signal fusion  -- blend competitor median + internal cost into fair value
    2. Pricing logic  -- a bid/ask-style spread around fair value, width chosen by a bandit
    3. Guardrails     -- deterministic, non-ML floor/ceiling/anti-collusion checks

The guardrail layer runs *after* the learned component and can only override
it, never be overridden by it. That ordering is the entire safety argument:
a bandit that has learned something stupid, or a competitor feed that has
been poisoned, still cannot produce a price below cost, below MAP, or
identical to a competitor's. Anything the model does is bounded by rules a
human wrote and can read.

Quotes are persisted to Postgres rather than held in a dict, because the
payment orchestrator runs in a *different process* and must be able to
re-validate the exact quote the storefront issued. An in-memory quote book
works right up until you scale the pricing service past one replica.
"""
from __future__ import annotations

import json
import logging
import statistics
import time

from agentmarket_core import db
from agentmarket_core.adapters.bus import EventBus
from agentmarket_core.adapters.featurestore import FeatureStore
from agentmarket_core.config import (
    TOPIC_QUOTE_ISSUED,
    TOPIC_TRANSACTION_OUTCOME,
    settings,
)
from agentmarket_core.domain.bandit import PersistentBandit
from agentmarket_core.models import PriceQuote

log = logging.getLogger("agentmarket.pricing")

PRICE_CEILING_MULTIPLE = 1.15   # never quote more than 15% over list
COMPETITOR_UNDERCUT = 0.98      # sit 2% under the competitor median
NO_COMPETITOR_DISCOUNT = 0.85   # fall back to 85% of list when blind
COLLUSION_EPSILON = 0.005       # "identical to a competitor" tolerance, in dollars
COLLUSION_NUDGE = 0.05


class PricingEngine:
    def __init__(
        self,
        store: FeatureStore,
        bus: EventBus | None = None,
        bandit: PersistentBandit | None = None,
        persist: bool = True,
    ) -> None:
        self.store = store
        self.bus = bus
        self.bandit = bandit or PersistentBandit()
        self.persist = persist
        # Fallback quote book, used only when persist=False (unit tests).
        self._quotes: dict[str, PriceQuote] = {}

    # -- 1. signal fusion ---------------------------------------------------
    def _fair_value(self, sku: str) -> tuple[float, float]:
        f = self.store.get_online(sku)
        internal_cost = f.get("internal_cost")
        list_price = f.get("list_price")
        if internal_cost is None:
            raise KeyError(f"no internal features for sku={sku!r}; run scripts/seed.py first")

        cost_floor = internal_cost * (1 + settings.min_margin_ratio)
        prices = f.get("competitor_prices") or []
        if prices:
            # Median, not mean: one surviving bad observation should not drag
            # fair value, and the outlier filter upstream is not infallible.
            fair_value = max(cost_floor, statistics.median(prices) * COMPETITOR_UNDERCUT)
        else:
            fair_value = max(cost_floor, (list_price or cost_floor) * NO_COMPETITOR_DISCOUNT)
        return fair_value, cost_floor

    # -- 2 + 3. pricing logic + guardrails ----------------------------------
    def quote(self, sku: str) -> PriceQuote:
        fair_value, cost_floor = self._fair_value(sku)
        f = self.store.get_online(sku)
        map_price = f.get("map_price")
        list_price = f.get("list_price")

        spread = self.bandit.choose(sku)
        ask = round(fair_value * (1 + spread), 2)

        guardrails: list[str] = []
        if map_price and ask < map_price:
            ask = round(float(map_price), 2)
            guardrails.append("MAP_FLOOR")
        if ask < cost_floor:
            ask = round(cost_floor, 2)
            guardrails.append("MARGIN_FLOOR")
        if list_price and ask > list_price * PRICE_CEILING_MULTIPLE:
            ask = round(list_price * PRICE_CEILING_MULTIPLE, 2)
            guardrails.append("PRICE_CEILING")

        # Anti-collusion: publishing a price that exactly mirrors a rival's
        # live price is the observable signature of tacit algorithmic
        # collusion, whether or not it was intended. Nudging off the match
        # costs five cents and keeps the pricing behaviour defensible.
        if any(abs(ask - cp) < COLLUSION_EPSILON for cp in (f.get("competitor_prices") or [])):
            ask = round(ask + COLLUSION_NUDGE, 2)
            guardrails.append("ANTI_COLLUSION_JITTER")

        quote = PriceQuote(
            sku=sku,
            amount=ask,
            spread=spread,
            fair_value=round(fair_value, 2),
            valid_until=time.time() + settings.quote_ttl_seconds,
            guardrails_applied=guardrails,
        )
        self._store_quote(quote)

        if self.bus:
            self.bus.publish(TOPIC_QUOTE_ISSUED, {
                "sku": sku, "quote_id": quote.quote_id, "amount": quote.amount,
                "spread": spread, "fair_value": quote.fair_value, "guardrails": guardrails,
            })
        return quote

    # -- quote book ---------------------------------------------------------
    def _store_quote(self, quote: PriceQuote) -> None:
        if not self.persist:
            self._quotes[quote.quote_id] = quote
            return
        db.execute(
            """INSERT INTO quotes (quote_id, sku, amount, currency, spread, fair_value,
                                   guardrails, issued_at, valid_until)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb, to_timestamp(%s), to_timestamp(%s))""",
            (quote.quote_id, quote.sku, quote.amount, quote.currency, quote.spread,
             quote.fair_value, json.dumps(quote.guardrails_applied),
             quote.issued_at, quote.valid_until),
        )

    def get_quote(self, quote_id: str) -> PriceQuote | None:
        if not self.persist:
            return self._quotes.get(quote_id)
        row = db.query_one(
            """SELECT quote_id, sku, amount::float AS amount, currency, spread::float AS spread,
                      fair_value::float AS fair_value, guardrails,
                      extract(epoch FROM issued_at) AS issued_at,
                      extract(epoch FROM valid_until) AS valid_until
                 FROM quotes WHERE quote_id = %s""",
            (quote_id,),
        )
        if not row:
            return None
        return PriceQuote(
            quote_id=row["quote_id"], sku=row["sku"], amount=row["amount"],
            currency=row["currency"], spread=row["spread"], fair_value=row["fair_value"],
            guardrails_applied=list(row["guardrails"] or []),
            issued_at=float(row["issued_at"]), valid_until=float(row["valid_until"]),
        )

    def is_quote_valid(self, quote_id: str, sku: str, amount: float) -> bool:
        """Re-validate a quote at settlement time.

        All three checks matter and each blocks a distinct attack: the SKU
        check stops a quote for a cheap item being spent on an expensive one,
        the amount check stops a tampered cart mandate, and the TTL stops an
        agent from sitting on a favourable quote until the market moves.
        """
        q = self.get_quote(quote_id)
        if not q or q.sku != sku:
            return False
        if abs(q.amount - amount) > COLLUSION_EPSILON:
            return False
        return time.time() <= q.valid_until

    # -- transaction-outcome feedback loop (proposal §4.1) ------------------
    def record_outcome(self, quote_id: str, won: bool) -> None:
        q = self.get_quote(quote_id)
        if not q:
            return
        internal_cost = self.store.get_online(q.sku).get("internal_cost") or 0.0
        # Reward is realised gross margin, not revenue: rewarding revenue
        # teaches the bandit that the widest spread is always best right up
        # until nobody buys anything.
        reward = (q.amount - internal_cost) if won else 0.0
        self.bandit.update(q.sku, q.spread, reward)
        if self.persist:
            db.execute(
                "UPDATE quotes SET outcome = %s, outcome_at = now() WHERE quote_id = %s",
                ("won" if won else "lost", quote_id),
            )
        if self.bus:
            self.bus.publish(TOPIC_TRANSACTION_OUTCOME, {
                "sku": q.sku, "quote_id": quote_id, "won": won, "reward": round(reward, 2),
                "spread": q.spread,
            })
