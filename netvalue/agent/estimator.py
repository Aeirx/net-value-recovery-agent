"""The agent's only source of physics: ``P̂(success | what it can see, what it does)``.

This module is what makes the whole submission non-circular. The value engine needs to
know how likely a retry is to work. It must not get that by reading ``world/recovery.py``
— then the agent and the simulator would share one model of the physics and the agent
would win by construction. Instead it gets what a real payments team has: a log of what a
previous, unintelligent system actually did and what happened (``data/history.jsonl``),
and it estimates the curves from that.

Two properties are load-bearing:

**It is cause-agnostic, on purpose.** The log records observables, the action, and the
outcome. It never records *why* a payment failed, because the historical system never knew.
So this estimator learns ``P(success | observables, action)`` and nothing finer. That is
not a shortcut — it is the situation every real dunning team is in — and it means the
Phase 7 belief update cannot get ``P(fail | cause, action)`` from here. It has to come
from the diagnoser's own model. Recorded in ``DECISIONS.md`` so it is not rediscovered on
Thursday.

**Sparse cells shrink toward their parents.** The full feature cross-product has far more
cells than the log has rows. A hierarchical beta-binomial along ``BACKOFF_LEVELS`` lets
every cell borrow strength from the coarser cell above it, weighted by ``shrinkage``
pseudo-observations. A cell with no data returns its parent exactly; a cell with a lot of
data ignores its parent. An error code the tuning regime never emitted — config B's
``GW_99`` — falls through to the per-intervention rate rather than crashing or, worse,
returning a confident number from nothing.

Boundary: this module imports nothing from ``netvalue.world``. ``tests/test_boundary.py``
enforces it; ``tests/test_world.py`` separately asserts the log carries no cause field.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from netvalue.agent.calibration import log_loss
from netvalue.agent.features import BACKOFF_LEVELS, Features, from_history_row, from_observation
from netvalue.agent.observation import Observation
from netvalue.agent.policy import ActionKind

#: Beta(1, 1) at the root: a flat prior, so the global rate is almost purely the data.
_ROOT_ALPHA = 1.0
_ROOT_BETA = 1.0

#: Draws used to turn a Beta posterior into a credible interval. Fixed seed, so the
#: interval is a deterministic function of the counts.
_CI_DRAWS = 4_000


@dataclass(frozen=True, slots=True)
class Estimate:
    """A probability with its uncertainty and its provenance."""

    p: float
    alpha: float
    beta: float
    #: 90% credible interval on the underlying success rate.
    low: float
    high: float
    #: Raw observations in the finest cell that had any. Zero means pure backoff.
    n_support: int
    #: How many rungs of the ladder had to be climbed to find data. 0 = the finest cell
    #: had observations; ``len(BACKOFF_LEVELS) - 1`` = nothing matched but the global rate.
    backoff_depth: int

    @property
    def effective_n(self) -> float:
        """Pseudo-observation count behind the estimate. Small means shrinkage dominates."""
        return self.alpha + self.beta

    @property
    def std(self) -> float:
        a, b = self.alpha, self.beta
        return math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1.0)))


@dataclass(frozen=True, slots=True)
class _Cell:
    successes: int
    failures: int

    @property
    def n(self) -> int:
        return self.successes + self.failures


class RecoveryEstimator:
    """Hierarchical beta-binomial over the feature backoff ladder."""

    def __init__(self, shrinkage: float = 20.0) -> None:
        if shrinkage <= 0.0:
            raise ValueError("shrinkage must be positive: it is a pseudo-observation count")
        self.shrinkage = shrinkage
        self._levels: list[dict[tuple[object, ...], _Cell]] = [
            {} for _ in BACKOFF_LEVELS
        ]
        self._fitted = False
        self._n_rows = 0

    # ------------------------------------------------------------------ fitting

    def fit(self, rows: Iterable[dict[str, Any]]) -> RecoveryEstimator:
        counts: list[defaultdict[tuple[object, ...], list[int]]] = [
            defaultdict(lambda: [0, 0]) for _ in BACKOFF_LEVELS
        ]
        n = 0
        for row in rows:
            features = from_history_row(row)
            succeeded = bool(row["succeeded"])
            for level_idx, names in enumerate(BACKOFF_LEVELS):
                key = features.as_tuple(*names)
                counts[level_idx][key][0 if succeeded else 1] += 1
            n += 1

        if n == 0:
            raise ValueError("cannot fit an estimator on an empty log")

        self._levels = [
            {key: _Cell(s, f) for key, (s, f) in level.items()} for level in counts
        ]
        self._n_rows = n
        self._fitted = True
        return self

    @classmethod
    def from_jsonl(cls, path: str | Path, shrinkage: float = 20.0) -> RecoveryEstimator:
        return cls(shrinkage).fit(_read_jsonl(path))

    @property
    def n_rows(self) -> int:
        return self._n_rows

    # --------------------------------------------------------------- prediction

    def _posterior(self, features: Features) -> tuple[float, float, int, int]:
        """Walk the ladder from the root down, shrinking each level toward its parent.

        Returns ``(alpha, beta, n_support, backoff_depth)``.
        """
        if not self._fitted:
            raise RuntimeError("estimator is not fitted")

        alpha, beta = _ROOT_ALPHA, _ROOT_BETA
        n_support = 0
        finest_with_data = len(BACKOFF_LEVELS) - 1

        # BACKOFF_LEVELS is finest-first; iterate coarsest-first so each rung's prior is
        # the rung above it.
        for level_idx in reversed(range(len(BACKOFF_LEVELS))):
            names = BACKOFF_LEVELS[level_idx]
            cell = self._levels[level_idx].get(features.as_tuple(*names))
            s = cell.successes if cell else 0
            f = cell.failures if cell else 0
            if not names:
                # The global level takes the flat root prior directly. Shrinking it
                # toward 0.5 with κ pseudo-observations was a real error: with a strong
                # κ and a modest log, every rung inherited a rate pulled toward a coin
                # flip rather than toward the data. A test caught it.
                alpha = s + _ROOT_ALPHA
                beta = f + _ROOT_BETA
            else:
                parent_mean = alpha / (alpha + beta)
                alpha = s + self.shrinkage * parent_mean
                beta = f + self.shrinkage * (1.0 - parent_mean)
            if cell and cell.n > 0:
                n_support = cell.n
                finest_with_data = level_idx

        return alpha, beta, n_support, finest_with_data

    def predict(self, features: Features) -> Estimate:
        alpha, beta, n_support, backoff_depth = self._posterior(features)
        p = alpha / (alpha + beta)
        draws = np.random.default_rng(0).beta(alpha, beta, _CI_DRAWS)
        low, high = np.quantile(draws, [0.05, 0.95])
        return Estimate(
            p=float(p),
            alpha=float(alpha),
            beta=float(beta),
            low=float(low),
            high=float(high),
            n_support=n_support,
            backoff_depth=backoff_depth,
        )

    def predict_for(
        self, observation: Observation, action: ActionKind, *, contact_index: int | None = None
    ) -> Estimate:
        """The call the value engine makes: "if I do this now, does it work?"."""
        if action is ActionKind.ABANDON:
            return Estimate(0.0, 0.0, 1.0, 0.0, 0.0, 0, 0)
        return self.predict(from_observation(observation, action, contact_index=contact_index))

    # ------------------------------------------------------------- introspection

    def supported_interventions(self) -> set[str]:
        """Interventions the log has *any* evidence for. The agent cannot price others."""
        level_idx = BACKOFF_LEVELS.index(("intervention",))
        return {str(key[0]) for key in self._levels[level_idx]}

    def cell_count(self, level_idx: int) -> int:
        return len(self._levels[level_idx])

    def describe(self) -> dict[str, Any]:
        return {
            "shrinkage": self.shrinkage,
            "rows": self._n_rows,
            "cells_per_level": [self.cell_count(i) for i in range(len(BACKOFF_LEVELS))],
            "levels": [list(names) for names in BACKOFF_LEVELS],
            "interventions": sorted(self.supported_interventions()),
        }


# ------------------------------------------------------------------- utilities


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def split_by_transaction(
    rows: Sequence[dict[str, Any]], *, validation_share: float = 0.25, seed: int = 20260902
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic train/validation split that keeps a transaction's rows together.

    Splitting rows independently would put attempt 1 of a transaction in training and
    attempt 2 in validation, and the two are not independent — that leaks. Splitting on
    the transaction id is the minimum that keeps the validation score honest.
    """
    if not 0.0 < validation_share < 1.0:
        raise ValueError("validation_share must be in (0, 1)")
    ids = sorted({str(r["transaction_id"]) for r in rows})
    rng = np.random.default_rng(seed)
    held = set(rng.choice(ids, size=int(len(ids) * validation_share), replace=False).tolist())
    train = [r for r in rows if r["transaction_id"] not in held]
    valid = [r for r in rows if r["transaction_id"] in held]
    return train, valid


def select_shrinkage(
    train: Sequence[dict[str, Any]],
    valid: Sequence[dict[str, Any]],
    grid: Sequence[float] = (2.0, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0),
) -> tuple[float, dict[float, float]]:
    """Pick the pseudo-count by validation log-loss. Empirical Bayes, done plainly.

    Returns the chosen value and the full curve, so the choice is auditable rather than a
    number that appeared in a config.
    """
    curve: dict[float, float] = {}
    for kappa in grid:
        est = RecoveryEstimator(kappa).fit(train)
        preds = [est.predict(from_history_row(r)).p for r in valid]
        labels = [bool(r["succeeded"]) for r in valid]
        curve[kappa] = log_loss(preds, labels)
    best = min(curve, key=lambda k: curve[k])
    return best, curve
