"""The net-value agent.

Ties the four pieces together, each of which was built and validated on its own:

    diagnosis  ->  belief  ->  value engine  ->  gates  ->  action
    (a posterior)  (updated   (backward       (can only
                   by failure) induction)      shrink)

Nothing here is novel; the novelty is in what it refuses to do. It does not hardcode an
attempt cap, it does not chase recovery rate, and it treats abandoning as an action to be
priced rather than a failure to be avoided.

**It never reads ground truth.** ``tests/test_boundary.py`` covers this file explicitly and
transitively — the estimator learned from a log with no causes, the diagnoser sees only the
observation, and the economics are the merchant's own books. That is what makes the result
a measurement rather than a tautology.

The audit row emitted on every decision carries the belief, the estimator's probability and
interval, the Q-value of *every* action considered, the gate that fired, and the arithmetic
behind the choice. That is the deliverable: it should be possible to point at any rupee,
spent or deliberately not spent, and follow the reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from netvalue.agent import bounds
from netvalue.agent.belief import Belief
from netvalue.agent.diagnose.schema import CausePosterior, DiagnosedCause, Diagnoser
from netvalue.agent.estimator import RecoveryEstimator
from netvalue.agent.observation import AttemptOutcome, Observation
from netvalue.agent.policy import Action, ActionKind
from netvalue.agent.value import Candidate, ValueEngine, ValueEngineConfig


@dataclass(slots=True)
class DecisionRecord:
    """One priced decision, kept whole. The audit log is built from these."""

    transaction_id: str
    at: datetime
    belief: str
    confidence: float
    entropy_bits: float
    chosen: str
    q_value: float
    immediate_value: float
    option_value: float
    p_success: float
    p_low: float
    p_high: float
    cost: float
    gate_fired: str
    runner_up: str
    runner_up_q: float
    rationale: str

    def as_row(self) -> dict[str, object]:
        return {
            "transaction_id": self.transaction_id,
            "at": self.at.isoformat(),
            "belief": self.belief,
            "confidence": round(self.confidence, 4),
            "entropy_bits": round(self.entropy_bits, 3),
            "action": self.chosen,
            "q_value_inr": round(self.q_value, 2),
            "immediate_value_inr": round(self.immediate_value, 2),
            "option_value_inr": round(self.option_value, 2),
            "p_success": round(self.p_success, 4),
            "p_success_ci": [round(self.p_low, 4), round(self.p_high, 4)],
            "cost_inr": round(self.cost, 2),
            "gate_fired": self.gate_fired,
            "runner_up": self.runner_up,
            "runner_up_q_inr": round(self.runner_up_q, 2),
            "rationale": self.rationale,
        }


@dataclass(slots=True)
class _TransactionState:
    belief: Belief
    last_debit_at: datetime | None = None
    seen_attempts: int = 0


class NetValuePolicy:
    """Optimises net value, and stops when continuing is not worth it."""

    name = "net_value"

    def __init__(
        self,
        diagnoser: Diagnoser,
        estimator: RecoveryEstimator,
        *,
        engine_config: ValueEngineConfig | None = None,
        bounds_config: bounds.BoundsConfig | None = None,
        reference_prior: dict[DiagnosedCause, float] | None = None,
    ) -> None:
        self.diagnoser = diagnoser
        self.engine = ValueEngine(estimator, engine_config, reference_prior)
        self.bounds_config = bounds_config or bounds.BoundsConfig()
        self._state: dict[str, _TransactionState] = {}
        self.decisions: list[DecisionRecord] = []

    # ------------------------------------------------------------------ belief state

    def _belief_for(self, observation: Observation) -> _TransactionState:
        """Diagnose once, then update on what the attempts revealed.

        Re-diagnosing every step would be defensible and, with an LLM arm, expensive. The
        Bayesian update is the cheap equivalent: it applies exactly the evidence the new
        attempts carry, which is what changed since the diagnosis was made.
        """
        state = self._state.get(observation.transaction_id)
        if state is None:
            state = _TransactionState(
                belief=Belief.from_diagnosis(self.diagnoser.diagnose(observation))
            )
            self._state[observation.transaction_id] = state

        # Fold in any attempts that have happened since this transaction was last seen.
        for prior in observation.prior_attempts[state.seen_attempts :]:
            if prior.outcome is AttemptOutcome.FAILED:
                kind = _ACTION_BY_NAME.get(prior.intervention)
                if kind is not None:
                    state.belief = state.belief.after_failure(kind)
            state.seen_attempts += 1
        return state

    # ------------------------------------------------------------------ the decision

    def decide(self, observation: Observation) -> Action:
        state = self._belief_for(observation)
        candidates = self.engine.evaluate(observation, state.belief)
        gated = bounds.apply(
            candidates,
            observation,
            state.belief,
            config=self.bounds_config,
            now=observation.observed_at,
            last_debit_at=state.last_debit_at,
        )

        best = max(gated.allowed, key=lambda c: c.q_value)
        runner_up = _runner_up(gated.allowed, best)

        self.decisions.append(
            DecisionRecord(
                transaction_id=observation.transaction_id,
                at=observation.observed_at,
                belief=state.belief.describe(),
                confidence=state.belief.confidence,
                entropy_bits=state.belief.entropy_bits,
                chosen=best.action.kind.value,
                q_value=best.q_value,
                immediate_value=best.immediate,
                option_value=best.continuation,
                p_success=best.p_success,
                p_low=best.p_estimate_low,
                p_high=best.p_estimate_high,
                cost=best.cost,
                gate_fired=gated.gate_names,
                runner_up=runner_up.action.kind.value if runner_up else "",
                runner_up_q=runner_up.q_value if runner_up else 0.0,
                rationale=best.explain(),
            )
        )

        if best.action.kind is not ActionKind.ABANDON:
            state.last_debit_at = observation.observed_at

        return Action(
            kind=best.action.kind,
            delay_hours=best.action.delay_hours,
            scheduled_at=best.action.scheduled_at,
            expected_value_inr=best.q_value,
            expected_cost_inr=best.cost,
            gate_fired=gated.gate_names or None,
            rationale=(
                f"{best.explain()} | belief: {state.belief.describe()}"
                + (f" | beat {runner_up.action.kind.value} "
                   f"by Rs {best.q_value - runner_up.q_value:,.0f}" if runner_up else "")
            ),
        )

    def reset(self) -> None:
        self._state.clear()
        self.decisions.clear()

    # ------------------------------------------------------------------ the artifacts

    def audit_rows(self) -> list[dict[str, object]]:
        return [d.as_row() for d in self.decisions]

    def deliberate_abandons(self) -> list[DecisionRecord]:
        """Transactions the agent stopped on while an alternative was still available.

        Filtered to decisions where abandoning *beat* a live option rather than being the
        only thing left — those are the judgment calls, and they are what the
        abandoned-but-recoverable list is built from.
        """
        return [
            d
            for d in self.decisions
            if d.chosen == ActionKind.ABANDON.value and d.runner_up and not d.gate_fired
        ]


def _runner_up(candidates: list[Candidate], best: Candidate) -> Candidate | None:
    others = [c for c in candidates if c is not best]
    return max(others, key=lambda c: c.q_value) if others else None


_ACTION_BY_NAME: dict[str, ActionKind] = {kind.value: kind for kind in ActionKind}


def reference_prior_from(
    diagnoser: Diagnoser, observations: list[Observation]
) -> dict[DiagnosedCause, float]:
    """The population cause mix, as the agent's own diagnoser sees it.

    This is what the estimator's marginals were implicitly averaged over, and it is the
    denominator of the likelihood-ratio reweighting. The agent computes it from its own
    diagnoser rather than being handed it — a merchant can look at its own decline mix,
    but nobody hands it the true causes.
    """
    if not observations:
        return {c: 1.0 / len(DiagnosedCause) for c in DiagnosedCause}
    totals = {c: 0.0 for c in DiagnosedCause}
    for obs in observations:
        posterior = diagnoser.diagnose(obs)
        for cause in DiagnosedCause:
            totals[cause] += posterior[cause]
    n = float(len(observations))
    return {c: v / n for c, v in totals.items()}


def build(
    diagnoser: Diagnoser,
    history_path: str = "data/history.jsonl",
    *,
    shrinkage: float = 20.0,
    depth: int = 4,
    reference_observations: list[Observation] | None = None,
) -> NetValuePolicy:
    """Assemble the agent from a diagnoser and the historical log."""
    estimator = RecoveryEstimator.from_jsonl(history_path, shrinkage=shrinkage)
    reference = (
        reference_prior_from(diagnoser, reference_observations)
        if reference_observations
        else None
    )
    return NetValuePolicy(
        diagnoser,
        estimator,
        engine_config=ValueEngineConfig(max_depth=depth),
        reference_prior=reference,
    )


def diagnosis_of(policy: NetValuePolicy, observation: Observation) -> CausePosterior:
    """The belief the agent currently holds. Used by the reporting layer, not by the agent."""
    return policy._belief_for(observation).belief.posterior
