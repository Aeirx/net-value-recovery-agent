"""Confidence intervals on the headline delta.

A single run produces a single number, and recovery is stochastic. If the net-value gap
over max-recovery is Rs 40k and the run-to-run standard deviation is Rs 35k, then the
result is noise reported as a finding — and without an interval there is no way to tell.

The interval is not a nicety deferred to a "future work" slide. It belongs to the number
being presented.

Two choices make it tighter and more honest:

* **Cluster on the transaction.** Replications of the same transaction are correlated, so
  resampling individual episodes would understate the variance. The bootstrap resamples
  *transactions*, carrying all their replications together.
* **Bootstrap the paired delta, not the two means.** Both policies faced the identical
  world at each ``(transaction, replication)``, so differencing first removes the shared
  variance instead of accumulating it twice.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class Interval:
    mean: float
    low: float
    high: float
    level: float
    n_clusters: int
    n_resamples: int

    @property
    def excludes_zero(self) -> bool:
        """Whether the sign of the effect is resolved at this confidence level."""
        return self.low > 0.0 or self.high < 0.0

    def format_inr(self) -> str:
        sign = "+" if self.mean >= 0 else ""
        return f"{sign}{self.mean:,.0f} [{self.low:,.0f}, {self.high:,.0f}]"

    def __str__(self) -> str:
        return f"{self.mean:,.2f} (95% CI {self.low:,.2f} to {self.high:,.2f})"


def cluster_bootstrap(
    values: dict[tuple[str, int], float],
    *,
    level: float = 0.95,
    n_resamples: int = 10_000,
    seed: int = 20260901,
) -> Interval:
    """Percentile bootstrap over transaction clusters.

    ``values`` is keyed by ``(transaction_id, replication)``; the transaction is the
    cluster. Returns the interval for the **total**, scaled to the observed number of
    clusters so the figure is comparable with the reported totals.
    """
    if not values:
        raise ValueError("nothing to bootstrap")

    clusters: defaultdict[str, list[float]] = defaultdict(list)
    for (txn_id, _replication), value in values.items():
        clusters[txn_id].append(value)

    ids = sorted(clusters)
    # Cluster totals, so a resample draws a transaction with all of its replications.
    totals = np.array([math.fsum(clusters[i]) for i in ids], dtype=float)
    n = len(totals)
    observed = float(totals.sum())

    if n < 2:
        return Interval(observed, observed, observed, level, n, 0)

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_resamples, n))
    means = totals[idx].mean(axis=1)
    draws = means * n  # rescale each resample to a total over n clusters

    alpha = (1.0 - level) / 2.0
    low, high = np.quantile(draws, [alpha, 1.0 - alpha])
    return Interval(observed, float(low), float(high), level, n, n_resamples)


def per_transaction(interval: Interval, n_transactions: int, n_replications: int) -> Interval:
    """Rescale a total interval to a per-transaction one, for cross-run comparability."""
    denom = max(n_transactions * n_replications, 1)
    return Interval(
        interval.mean / denom,
        interval.low / denom,
        interval.high / denom,
        interval.level,
        interval.n_clusters,
        interval.n_resamples,
    )
