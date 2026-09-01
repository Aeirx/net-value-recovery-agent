"""Scoring a diagnoser the way the economics actually score it.

A plain confusion matrix weights every mistake the same. In this world they are wildly
unequal:

* Calling a dead mandate an expired card costs one wasted customer contact — a couple of
  rupees of comms plus the annoyance.
* Calling an expired card a dead mandate costs the **entire recovery**, because you
  abandon a subscription that a single card-update request would have saved.

Those differ by two orders of magnitude, and an accuracy figure reports them identically.
So this module builds a **regret matrix in rupees**: for each ``(true cause, diagnosed
cause)`` pair, how much net value is destroyed by acting on the diagnosis instead of the
truth. Cost-weighted error is then the number that matters, and raw accuracy is reported
beside it only so the two can be compared.

The second metric is **posterior calibration**. The value engine consumes the diagnoser's
confidence directly, so a diagnoser that says 90% and is right 60% of the time will make
the agent spend money it should not. That is invisible to accuracy and fatal downstream.

Lives in ``eval/`` because it needs ground truth. The agent never sees any of it.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from netvalue.agent.calibration import (
    ReliabilityBin,
    expected_calibration_error,
    reliability,
)
from netvalue.agent.diagnose.schema import CausePosterior, DiagnosedCause
from netvalue.world.banks import build_world_health
from netvalue.world.config import Cause, Intervention, Rail, Segment, WorldConfig
from netvalue.world.recovery import RecoveryContext, debit_success_probability

#: The single best lever for each cause, if you knew the cause for certain. Regret is
#: measured against playing this and getting it wrong.
IDEAL_ACTION: dict[Cause, Intervention] = {
    Cause.INSUFFICIENT_FUNDS: Intervention.SCHEDULE_RETRY_AT,
    Cause.BANK_OUTAGE: Intervention.RETRY_AFTER,
    Cause.ROUTE_DEGRADED: Intervention.SWITCH_ROUTE_AND_RETRY,
    Cause.AFA_TIMEOUT: Intervention.RETRY_NOW,
    Cause.CARD_EXPIRED: Intervention.REQUEST_CARD_UPDATE,
    Cause.RISK_BLOCK: Intervention.ESCALATE_TO_HUMAN,
    Cause.MANDATE_DEAD: Intervention.ABANDON,
}

#: A representative transaction the matrix is computed against: the median plan, the
#: middle engagement segment, the card rail (the only one where every cause is possible),
#: at a mid-cycle moment with the world healthy. One interpretable matrix rather than a
#: different one per transaction.
_REF_AMOUNT = 149.0
_REF_SEGMENT = Segment.LAPSED
_REF_WHEN = datetime(2026, 3, 18, 10, 0)


@dataclass(frozen=True, slots=True)
class DiagnosisReport:
    diagnoser: str
    n: int
    accuracy: float
    top2_accuracy: float
    mean_regret_inr: float
    total_regret_inr: float
    mean_confidence: float
    mean_entropy_bits: float
    ece: float
    confusion: dict[str, dict[str, int]]
    regret_by_pair: dict[str, float]
    worst_confusions: list[tuple[str, str, int, float]]

    def summary(self) -> dict[str, object]:
        return {
            "diagnoser": self.diagnoser,
            "n": self.n,
            "accuracy": round(self.accuracy, 4),
            "top2_accuracy": round(self.top2_accuracy, 4),
            "mean_regret_inr": round(self.mean_regret_inr, 2),
            "total_regret_inr": round(self.total_regret_inr, 2),
            "mean_confidence": round(self.mean_confidence, 4),
            "mean_entropy_bits": round(self.mean_entropy_bits, 3),
            "confidence_ece": round(self.ece, 4),
            "worst_confusions": [
                {"true": t, "diagnosed": d, "n": n, "regret_inr": round(r, 2)}
                for t, d, n, r in self.worst_confusions
            ],
        }


def _expected_value(
    cfg: WorldConfig, true_cause: Cause, action: Intervention, ltv: float
) -> float:
    """Net value of playing ``action`` when the truth is ``true_cause``, for the reference
    transaction. Uses the world's physics because this is evaluation, not the agent."""
    if action is Intervention.ABANDON:
        return 0.0

    spec = cfg.costs.interventions[action]
    value = cfg.recovery_value(_REF_AMOUNT, Rail.CARD_MANDATE, ltv)
    cost = spec.flat_cost_inr
    if spec.consumes_contact:
        cost += cfg.annoyance_cost(1, ltv)

    if action is Intervention.REQUEST_CARD_UPDATE:
        p = 0.0
        if true_cause is Cause.CARD_EXPIRED:
            base = cfg.segments[_REF_SEGMENT].contact_response_prob
            p = cfg.costs.card_update.response_prob(base, 1)
            p *= cfg.costs.card_update.p_success_given_response
    elif action is Intervention.ESCALATE_TO_HUMAN:
        p = (
            cfg.costs.escalation.p_resolves_risk_block
            if true_cause is Cause.RISK_BLOCK
            else cfg.costs.escalation.p_resolves_other_cause
        )
        if true_cause is Cause.MANDATE_DEAD:
            p = 0.0
    else:
        ctx = RecoveryContext(
            transaction_id="REF", true_cause=true_cause, rail=Rail.CARD_MANDATE,
            amount_inr=_REF_AMOUNT, segment=_REF_SEGMENT, bank_id="BK_HDFC",
            route="RT_ALPHA", when=_REF_WHEN, attempt_index=1, contact_index=1,
            health=build_world_health(cfg),
        )
        p = debit_success_probability(cfg, ctx, action)

    return p * value - cost


def regret_matrix(cfg: WorldConfig) -> dict[tuple[Cause, Cause], float]:
    """``regret[true, diagnosed]`` — net value destroyed by acting on the wrong diagnosis.

    Zero on the diagonal by construction. Never negative: acting on a wrong belief cannot
    beat acting on the truth.
    """
    ltv = cfg.ltv_remaining(_REF_AMOUNT, _REF_SEGMENT)
    best = {c: _expected_value(cfg, c, IDEAL_ACTION[c], ltv) for c in Cause}
    out: dict[tuple[Cause, Cause], float] = {}
    for true_cause in Cause:
        for diagnosed in Cause:
            got = _expected_value(cfg, true_cause, IDEAL_ACTION[diagnosed], ltv)
            out[(true_cause, diagnosed)] = max(0.0, best[true_cause] - got)
    return out


def evaluate(
    cfg: WorldConfig,
    name: str,
    posteriors: Sequence[CausePosterior],
    truths: Sequence[str],
) -> DiagnosisReport:
    if len(posteriors) != len(truths):
        raise ValueError("posteriors and truths differ in length")
    if not posteriors:
        raise ValueError("nothing to evaluate")

    matrix = regret_matrix(cfg)
    confusion: defaultdict[str, Counter[str]] = defaultdict(Counter)
    regrets: list[float] = []
    correct = 0
    top2 = 0
    confidences: list[float] = []
    hits: list[bool] = []

    for posterior, truth in zip(posteriors, truths, strict=True):
        true_cause: Cause = Cause(truth)
        predicted: Cause = Cause(posterior.top.value)
        confusion[truth][predicted.value] += 1
        regrets.append(matrix[(true_cause, predicted)])
        confidences.append(posterior.confidence)
        is_hit = predicted is true_cause
        hits.append(is_hit)
        correct += int(is_hit)
        if truth in {c.value for c, _ in posterior.ranked()[:2]}:
            top2 += 1

    pair_regret: dict[str, float] = {}
    worst: list[tuple[str, str, int, float]] = []
    for truth, counts in confusion.items():
        for guess, n in counts.items():
            if truth == guess:
                continue
            total = matrix[(Cause(truth), Cause(guess))] * n
            pair_regret[f"{truth}->{guess}"] = round(total, 2)
            worst.append((truth, guess, n, total))
    worst.sort(key=lambda x: x[3], reverse=True)

    return DiagnosisReport(
        diagnoser=name,
        n=len(posteriors),
        accuracy=correct / len(posteriors),
        top2_accuracy=top2 / len(posteriors),
        mean_regret_inr=math.fsum(regrets) / len(regrets),
        total_regret_inr=math.fsum(regrets),
        mean_confidence=math.fsum(confidences) / len(confidences),
        mean_entropy_bits=math.fsum(p.entropy_bits for p in posteriors) / len(posteriors),
        ece=expected_calibration_error(confidences, hits),
        confusion={t: dict(c) for t, c in confusion.items()},
        regret_by_pair=pair_regret,
        worst_confusions=worst[:6],
    )


def confidence_reliability(
    posteriors: Sequence[CausePosterior], truths: Sequence[str], *, n_bins: int = 10
) -> list[ReliabilityBin]:
    """Is the diagnoser's stated confidence honest? The question accuracy cannot answer."""
    confidences = [p.confidence for p in posteriors]
    hits = [p.top.value == t for p, t in zip(posteriors, truths, strict=True)]
    return reliability(confidences, hits, n_bins=n_bins)


def format_confusion(report: DiagnosisReport) -> str:
    causes = [c.value for c in DiagnosedCause]
    width = max(len(c) for c in causes) + 2
    header = " " * width + "".join(f"{c[:8]:>10}" for c in causes)
    lines = [header]
    for truth in causes:
        row = report.confusion.get(truth, {})
        cells = "".join(f"{row.get(c, 0):>10}" for c in causes)
        lines.append(f"{truth:<{width}}{cells}")
    return "\n".join(lines)
