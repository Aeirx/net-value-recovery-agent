"""Aggregating episodes into the numbers that go in the table.

Net value is the headline. Everything else is supporting evidence, and two of the
supporting columns exist specifically to make the thesis falsifiable rather than merely
assertable:

* ``gross_recovered_inr`` — the column the agent is *expected to lose on*. Reporting it
  next to net value is what turns "we optimise something better" from a slogan into a
  visible, quantified trade.
* ``abandoned_but_recoverable`` — transactions walked away from on purpose that would in
  fact have worked. A policy with zero of these is not exercising judgment.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from netvalue.eval.runner import EpisodeResult


@dataclass(frozen=True, slots=True)
class PolicyMetrics:
    policy: str
    n_transactions: int
    n_replications: int

    net_value_inr: float
    gross_recovered_inr: float
    attempt_cost_inr: float
    annoyance_cost_inr: float

    recovery_rate: float
    total_attempts: int
    total_contacts: int
    cost_per_recovery_inr: float
    wasted_attempts: int
    abandoned_but_recoverable: int
    gate_fires: int
    terminal_reasons: dict[str, int]

    @property
    def net_value_per_transaction(self) -> float:
        denom = self.n_transactions * max(self.n_replications, 1)
        return self.net_value_inr / denom if denom else 0.0

    @property
    def gross_per_transaction(self) -> float:
        denom = self.n_transactions * max(self.n_replications, 1)
        return self.gross_recovered_inr / denom if denom else 0.0


def summarise(policy: str, results: Sequence[EpisodeResult]) -> PolicyMetrics:
    if not results:
        raise ValueError("no episodes to summarise")

    replications = len({r.replication for r in results})
    transactions = len({r.transaction_id for r in results})
    recovered = [r for r in results if r.recovered]

    attempt_cost = math.fsum(r.attempt_cost_inr for r in results)
    annoyance_cost = math.fsum(r.annoyance_cost_inr for r in results)

    # An attempt is wasted if it was spent on an episode that never recovered. It is the
    # cleanest available proxy for effort that bought nothing.
    wasted = sum(r.attempts + r.contacts for r in results if not r.recovered)

    return PolicyMetrics(
        policy=policy,
        n_transactions=transactions,
        n_replications=replications,
        net_value_inr=math.fsum(r.net_value_inr for r in results),
        gross_recovered_inr=math.fsum(r.recovered_value_inr for r in results),
        attempt_cost_inr=attempt_cost,
        annoyance_cost_inr=annoyance_cost,
        recovery_rate=len(recovered) / len(results),
        total_attempts=sum(r.attempts for r in results),
        total_contacts=sum(r.contacts for r in results),
        cost_per_recovery_inr=(
            (attempt_cost + annoyance_cost) / len(recovered) if recovered else float("inf")
        ),
        wasted_attempts=wasted,
        abandoned_but_recoverable=sum(1 for r in results if r.abandoned_but_recoverable),
        gate_fires=sum(r.gate_fires for r in results),
        terminal_reasons=dict(Counter(r.terminal_reason for r in results)),
    )


def paired_deltas(
    treatment: Sequence[EpisodeResult], control: Sequence[EpisodeResult]
) -> dict[tuple[str, int], float]:
    """Per-transaction, per-replication net-value differences.

    Pairing is the whole point. Comparing two independent means over a stochastic world
    would bury a real effect under variance that both policies share; differencing within
    ``(transaction, replication)`` removes exactly that shared component, because both
    policies faced the identical realised world there.
    """
    t_index = {(r.transaction_id, r.replication): r for r in treatment}
    c_index = {(r.transaction_id, r.replication): r for r in control}
    shared = t_index.keys() & c_index.keys()
    if not shared:
        raise ValueError("no shared (transaction, replication) pairs to compare")
    return {k: t_index[k].net_value_inr - c_index[k].net_value_inr for k in sorted(shared)}


def win_rate(deltas: dict[tuple[str, int], float], tolerance: float = 1e-9) -> float:
    """Share of paired comparisons the treatment strictly wins.

    Reported alongside the mean because they answer different questions: a mean can be
    carried by a handful of large transactions, while the win rate says whether the policy
    is better *typically*. Where the two disagree, that disagreement is the finding.
    """
    wins = sum(1 for d in deltas.values() if d > tolerance)
    ties = sum(1 for d in deltas.values() if abs(d) <= tolerance)
    contested = len(deltas) - ties
    return wins / contested if contested else 0.0
