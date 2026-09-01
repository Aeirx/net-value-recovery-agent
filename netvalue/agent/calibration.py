"""Calibration metrics for probabilistic predictions.

Accuracy is the wrong yardstick for the estimator. The value engine consumes its
probabilities *directly*: it multiplies ``P̂`` by rupees and compares the product against
a cost. An estimator that says 70% and is right 50% of the time will make the agent spend
money it should not, and no accuracy figure would reveal that. Calibration is the property
that actually matters, so it is the property that is measured.

Three numbers, all standard:

* **Brier score** — mean squared error of the probability. Lower is better; a constant
  prediction of the base rate scores ``p(1-p)``, so anything above that is worse than
  knowing nothing.
* **Log-loss** — the proper scoring rule the shrinkage is tuned on.
* **Expected calibration error (ECE)** — bin predictions by confidence and measure the
  gap between what was predicted and what happened, weighted by bin size. This is the one
  that answers "when it says 70%, is it right 70% of the time?".

Lives under ``agent/`` because it depends on nothing but numbers, and putting it in
``eval/`` would make ``agent/`` import ``eval/`` while ``eval/`` already imports ``agent/``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

_EPS = 1e-9


def brier_score(predictions: Sequence[float], labels: Sequence[bool]) -> float:
    _check(predictions, labels)
    return math.fsum((p - float(y)) ** 2 for p, y in zip(predictions, labels, strict=True)) / len(
        labels
    )


def log_loss(predictions: Sequence[float], labels: Sequence[bool]) -> float:
    _check(predictions, labels)
    total = 0.0
    for p, y in zip(predictions, labels, strict=True):
        q = min(max(p, _EPS), 1.0 - _EPS)
        total += -math.log(q) if y else -math.log(1.0 - q)
    return total / len(labels)


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    low: float
    high: float
    count: int
    mean_predicted: float
    observed_rate: float

    @property
    def gap(self) -> float:
        return self.observed_rate - self.mean_predicted


def reliability(
    predictions: Sequence[float], labels: Sequence[bool], *, n_bins: int = 10
) -> list[ReliabilityBin]:
    """Equal-width bins over [0, 1]. Empty bins are omitted rather than reported as zero."""
    _check(predictions, labels)
    if n_bins < 2:
        raise ValueError("need at least two bins")
    sums: list[list[float]] = [[0.0, 0.0, 0.0] for _ in range(n_bins)]  # n, sum_p, sum_y
    for p, y in zip(predictions, labels, strict=True):
        idx = min(int(p * n_bins), n_bins - 1)
        sums[idx][0] += 1.0
        sums[idx][1] += p
        sums[idx][2] += float(y)
    out: list[ReliabilityBin] = []
    for i, (n, sp, sy) in enumerate(sums):
        if n == 0:
            continue
        out.append(
            ReliabilityBin(
                low=i / n_bins,
                high=(i + 1) / n_bins,
                count=int(n),
                mean_predicted=sp / n,
                observed_rate=sy / n,
            )
        )
    return out


def expected_calibration_error(
    predictions: Sequence[float], labels: Sequence[bool], *, n_bins: int = 10
) -> float:
    bins = reliability(predictions, labels, n_bins=n_bins)
    total = sum(b.count for b in bins)
    if total == 0:
        return 0.0
    return math.fsum(abs(b.gap) * b.count for b in bins) / total


def base_rate(labels: Sequence[bool]) -> float:
    if not labels:
        raise ValueError("no labels")
    return sum(1 for y in labels if y) / len(labels)


def _check(predictions: Sequence[float], labels: Sequence[bool]) -> None:
    if len(predictions) != len(labels):
        raise ValueError("predictions and labels differ in length")
    if not labels:
        raise ValueError("nothing to score")
    for p in predictions:
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"prediction out of range: {p}")
