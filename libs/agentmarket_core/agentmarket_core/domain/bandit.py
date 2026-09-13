"""Epsilon-greedy spread tuner, persisted to Postgres (proposal §4.2).

The engine has to choose how wide a spread to quote over fair value. Too
narrow and it leaves margin on the table; too wide and the agent buys from
someone else. There is no labelled training set for this -- the only signal
is whether a quote converted -- so it is a bandit problem, not a supervised
one.

Persisting arm statistics is what separates this from the MVP's in-memory
version. A policy that resets on every deploy never accumulates enough pulls
to beat random, and in a rolling deployment each replica would other wise
explore against its own private history while quoting from a shared price
book. The table is the shared memory.

Epsilon-greedy rather than something cleverer (UCB, Thompson) because it is
the one whose behaviour is obvious from the table contents during a demo,
and the exploration/exploitation gap between them is small at this scale.
"""
from __future__ import annotations

import random

from agentmarket_core import db
from agentmarket_core.config import settings


class PersistentBandit:
    def __init__(self, epsilon: float | None = None, arms: list[float] | None = None) -> None:
        self.epsilon = settings.bandit_epsilon if epsilon is None else epsilon
        self.arms = arms or settings.spread_candidates

    def _stats(self, sku: str) -> dict[float, tuple[int, float]]:
        rows = db.query(
            "SELECT spread::float AS spread, pulls, reward_sum::float AS reward_sum "
            "FROM bandit_arms WHERE sku = %s",
            (sku,),
        )
        return {r["spread"]: (r["pulls"], r["reward_sum"]) for r in rows}

    def choose(self, sku: str) -> float:
        """Pick a spread. Explore with probability epsilon; otherwise take the
        arm with the best mean reward so far, treating an arm that has never
        been pulled as unknown rather than bad (so every arm gets tried)."""
        if random.random() < self.epsilon:
            return random.choice(self.arms)

        stats = self._stats(sku)
        untried = [a for a in self.arms if stats.get(a, (0, 0.0))[0] == 0]
        if untried:
            return random.choice(untried)

        def mean_reward(arm: float) -> float:
            pulls, total = stats.get(arm, (0, 0.0))
            return total / pulls if pulls else 0.0

        return max(self.arms, key=mean_reward)

    def update(self, sku: str, spread: float, reward: float) -> None:
        """Record the outcome of one pull.

        The UPSERT is atomic and additive (`pulls + 1`, `reward_sum + x`)
        rather than read-modify-write, so concurrent pricing replicas
        updating the same arm cannot lose each other's observations.
        """
        db.execute(
            """INSERT INTO bandit_arms (sku, spread, pulls, reward_sum, updated_at)
               VALUES (%s, %s, 1, %s, now())
               ON CONFLICT (sku, spread) DO UPDATE
                 SET pulls      = bandit_arms.pulls + 1,
                     reward_sum = bandit_arms.reward_sum + EXCLUDED.reward_sum,
                     updated_at = now()""",
            (sku, spread, reward),
        )

    def snapshot(self, sku: str) -> list[dict]:
        """Arm statistics for the Ops Dashboard."""
        stats = self._stats(sku)
        return [
            {
                "spread": arm,
                "pulls": stats.get(arm, (0, 0.0))[0],
                "mean_reward": round(stats.get(arm, (0, 0.0))[1] / stats[arm][0], 4)
                if stats.get(arm, (0, 0.0))[0] else 0.0,
            }
            for arm in self.arms
        ]
