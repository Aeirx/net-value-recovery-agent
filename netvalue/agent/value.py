"""The value engine — finite-horizon backward induction over a belief state.

**This is the submission.** Everything else exists so that this can be trusted.

Every competing entry hardcodes ``max_attempts = 3``. The v2 plan called it the laziest
line in the field, and replacing it is the whole point: continue only while the value of
continuing exceeds the cost of continuing, where "the value of continuing" is computed
properly rather than asserted.

**Why not a one-step rule.** The obvious implementation — take an action if its immediate
expected value is positive — is greedy, and it is wrong in exactly the cases this project
is about. Waiting out a bank outage looks like pure cost with no immediate payoff, so a
myopic agent abandons a transaction it should have held. A cheap action whose entire value
is that it *unlocks a later one* is invisible to it.

Solving backward from the expiry horizon fixes that, and it fixes it structurally: the
continuation term ``(1 - p) x V(next state)`` **is** the option value. It is not a term
anyone had to invent and tune — it falls out of the recursion, which is why it can be
trusted at all.

**On the depth limit.** The belief is continuous, so an exact solve over the whole belief
simplex is not available. What is available is that the reachable beliefs form a tree
indexed by the action sequence, and the bounds cap that sequence at a handful of moves. The
search is therefore exact expectimax to ``max_depth`` and truncates below it, which is a
real approximation and is stated as one. Depth 1 is the greedy rule; depth 3-4 captures the
wait-then-act patterns that motivate the whole design.
``tests/test_dp.py`` checks the recursion against brute force on small instances.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from netvalue.agent.belief import Belief
from netvalue.agent.diagnose.schema import DiagnosedCause
from netvalue.agent.economics import CONTACT_ACTIONS, Economics
from netvalue.agent.estimator import RecoveryEstimator
from netvalue.agent.features import days_to_nearest_salary_day
from netvalue.agent.observation import Observation, ObservedRail
from netvalue.agent.policy import Action, ActionKind

#: Delays the engine will consider for a scheduled retry, in hours. The 24h floor is the
#: pre-debit notification window — anything shorter is not merely aggressive, it is
#: non-compliant, so it is not in the choice set at all.
RETRY_DELAYS_H: tuple[float, ...] = (24.0, 48.0, 72.0)

#: Causes a card-update request could possibly fix. Everything else makes it pure cost.
_CARD_UPDATE_FIXES = frozenset({DiagnosedCause.CARD_EXPIRED})

#: Causes a human escalation can move.
_ESCALATION_FIXES = frozenset({DiagnosedCause.RISK_BLOCK})

#: Causes no debit can ever fix, however well timed.
_DEBIT_CANNOT_FIX = frozenset({DiagnosedCause.MANDATE_DEAD, DiagnosedCause.CARD_EXPIRED})

#: How far the belief is allowed to move the estimator's number. A ratio far from 1 means
#: this transaction looks nothing like the log average, and the estimator is being asked to
#: extrapolate — so the adjustment is bounded rather than trusted without limit.
_REWEIGHT_CLIP = (0.15, 8.0)

#: Fallback reference mix, used only when no diagnoser sample is supplied. Deliberately
#: near-uniform: if the agent does not know the population it should not pretend to.
_DEFAULT_REFERENCE: dict[DiagnosedCause, float] = {c: 1.0 / len(DiagnosedCause) for c in DiagnosedCause}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One action, fully priced, with the arithmetic kept for the audit log."""

    action: Action
    p_success: float
    value_if_success: float
    cost: float
    continuation: float
    q_value: float
    p_estimate_low: float = 0.0
    p_estimate_high: float = 0.0

    @property
    def immediate(self) -> float:
        """What a greedy one-step rule would have scored. Reported beside ``q_value`` so
        the contribution of lookahead is visible rather than claimed."""
        return self.p_success * self.value_if_success - self.cost

    def explain(self) -> str:
        return (
            f"{self.action.kind.value}: P={self.p_success:.1%} x Rs {self.value_if_success:,.0f} "
            f"- Rs {self.cost:,.2f} + Rs {self.continuation:,.2f} option = "
            f"Rs {self.q_value:,.2f}"
        )


@dataclass(frozen=True, slots=True)
class DecisionState:
    """Everything the recursion carries. Time is the binding constraint, not attempts."""

    belief: Belief
    attempts_used: int
    contacts_used: int
    now: datetime
    route_switched: bool = False


@dataclass(slots=True)
class ValueEngineConfig:
    max_depth: int = 4
    max_attempts: int = 6
    max_debits_per_cycle: int = 3
    max_contacts: int = 3
    min_inter_attempt_hours: float = 24.0
    #: Beyond this the recursion stops expanding; the branch is worth nothing further.
    prune_below_inr: float = 0.0
    retry_delays_h: tuple[float, ...] = field(default_factory=lambda: RETRY_DELAYS_H)


class ValueEngine:
    """Prices every admissible action and returns them ranked."""

    def __init__(
        self,
        estimator: RecoveryEstimator,
        config: ValueEngineConfig | None = None,
        reference_prior: dict[DiagnosedCause, float] | None = None,
    ) -> None:
        self.estimator = estimator
        self.config = config or ValueEngineConfig()
        #: The cause mix the estimator implicitly averaged over. Needed because its
        #: probabilities are marginals, and this transaction is not the average one.
        self.reference_prior = reference_prior or dict(_DEFAULT_REFERENCE)

    # ------------------------------------------------------------------ action set

    def _admissible(
        self, observation: Observation, state: DecisionState
    ) -> list[Action]:
        """Actions the rules permit here. ``abandon`` is always among them: it is a real
        competing option, never a fallback for when nothing else is left."""
        cfg = self.config
        out: list[Action] = [Action(kind=ActionKind.ABANDON, rationale="stop")]
        hours_left = max(0.0, (observation.expires_at - state.now).total_seconds() / 3600.0)

        debit_budget = min(cfg.max_attempts, cfg.max_debits_per_cycle)
        if state.attempts_used < debit_budget:
            for delay in cfg.retry_delays_h:
                if delay < hours_left:
                    out.append(
                        Action(
                            kind=ActionKind.RETRY_AFTER,
                            delay_hours=delay,
                            rationale=f"retry in {delay:.0f}h",
                        )
                    )
            payday = self._next_payday(state.now, observation.expires_at)
            if payday is not None:
                gap = (payday - state.now).total_seconds() / 3600.0
                if gap >= cfg.min_inter_attempt_hours:
                    out.append(
                        Action(
                            kind=ActionKind.SCHEDULE_RETRY_AT,
                            scheduled_at=payday,
                            rationale="wait for the salary credit",
                        )
                    )
            if (
                observation.rail is ObservedRail.CARD_MANDATE
                and not state.route_switched
                and cfg.min_inter_attempt_hours < hours_left
            ):
                out.append(
                    Action(
                        kind=ActionKind.SWITCH_ROUTE_AND_RETRY,
                        delay_hours=cfg.min_inter_attempt_hours,
                        rationale="try the other acquirer route",
                    )
                )

        if state.contacts_used < cfg.max_contacts:
            if observation.rail is ObservedRail.CARD_MANDATE:
                out.append(
                    Action(
                        kind=ActionKind.REQUEST_CARD_UPDATE,
                        rationale="ask the customer to update their card",
                    )
                )
            out.append(
                Action(kind=ActionKind.ESCALATE_TO_HUMAN, rationale="escalate to a human")
            )
        return out

    @staticmethod
    def _next_payday(now: datetime, expires_at: datetime) -> datetime | None:
        for offset in range(0, 32):
            day = (now + timedelta(days=offset)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            if day <= now:
                continue
            if day >= expires_at:
                return None
            if days_to_nearest_salary_day(day) == 0:
                return day
        return None

    # ------------------------------------------------------------------ probabilities

    def _reweight(self, belief: Belief, action: ActionKind) -> float:
        """How much this transaction's belief should move the estimator's marginal.

        The estimator learned ``P(success | card-update)`` from a log in which most cards
        were not in fact expired, so its number already averages over that. Multiplying it
        by the belief mass again would discount the same fact twice — which is exactly the
        bug this replaces: on a transaction believed 42% expired, the naive product landed
        roughly nine times too low and the agent declined contacts it should have made.

        The correct adjustment is a likelihood ratio: how much more (or less) likely the
        fixable cause is *here* than in the population the estimator averaged over.
        """
        if action is ActionKind.REQUEST_CARD_UPDATE:
            relevant = _CARD_UPDATE_FIXES
        elif action is ActionKind.ESCALATE_TO_HUMAN:
            relevant = _ESCALATION_FIXES
        else:
            relevant = frozenset(set(DiagnosedCause) - set(_DEBIT_CANNOT_FIX))

        here = belief.mass_on(relevant)
        reference = sum(self.reference_prior.get(c, 0.0) for c in relevant)
        if reference <= 0.0:
            return 1.0
        low, high = _REWEIGHT_CLIP
        return min(max(here / reference, low), high)

    def _p_success(
        self, observation: Observation, state: DecisionState, action: Action
    ) -> tuple[float, float, float]:
        """``P(this action recovers the payment)``: the estimator's marginal, reweighted by
        how unusual this transaction's belief is.

        The estimator is cause-agnostic — it learned from a log that never recorded causes
        — so it prices "a card-update request on a transaction like this on average". The
        belief supplies what it structurally cannot: whether *this* card is the problem.
        The likelihood ratio is where diagnosis actually pays for itself.
        """
        est = self.estimator.predict_for(
            observation, action.kind, contact_index=state.contacts_used + 1
        )
        p = est.p * self._reweight(state.belief, action.kind)
        return min(max(p, 0.0), 1.0), est.low, est.high

    # ------------------------------------------------------------------ the recursion

    def _advance(self, state: DecisionState, action: Action) -> DecisionState:
        delay = action.delay_hours or 0.0
        if action.scheduled_at is not None:
            delay = max(0.0, (action.scheduled_at - state.now).total_seconds() / 3600.0)
        is_contact = action.kind in CONTACT_ACTIONS
        return DecisionState(
            belief=state.belief.after_failure(
                action.kind,
                days_to_payday=days_to_nearest_salary_day(state.now + timedelta(hours=delay)),
            ),
            attempts_used=state.attempts_used + (0 if is_contact else 1),
            contacts_used=state.contacts_used + (1 if is_contact else 0),
            now=state.now + timedelta(hours=delay),
            route_switched=state.route_switched
            or action.kind is ActionKind.SWITCH_ROUTE_AND_RETRY,
        )

    def _value(
        self, observation: Observation, state: DecisionState, economics: Economics, depth: int
    ) -> float:
        """``V(state)`` — the best achievable value from here, abandoning included.

        Never negative: ``abandon`` scores exactly zero and is always available, so the
        engine can always decline to spend. That is the property that makes walking away
        a decision rather than a failure.
        """
        if depth <= 0 or state.now >= observation.expires_at:
            return 0.0
        best = 0.0
        for candidate in self._candidates(observation, state, economics, depth):
            best = max(best, candidate.q_value)
        return best

    def _candidates(
        self, observation: Observation, state: DecisionState, economics: Economics, depth: int
    ) -> list[Candidate]:
        out: list[Candidate] = []
        value = economics.value_of_recovery

        for action in self._admissible(observation, state):
            if action.kind is ActionKind.ABANDON:
                out.append(
                    Candidate(
                        action=action, p_success=0.0, value_if_success=0.0,
                        cost=0.0, continuation=0.0, q_value=0.0,
                    )
                )
                continue

            p, low, high = self._p_success(observation, state, action)
            cost = economics.cost_of(action.kind, contact_index=state.contacts_used + 1)

            continuation = 0.0
            if depth > 1:
                nxt = self._advance(state, action)
                if nxt.now < observation.expires_at:
                    continuation = (1.0 - p) * self._value(
                        observation, nxt, economics, depth - 1
                    )

            out.append(
                Candidate(
                    action=action,
                    p_success=p,
                    value_if_success=value,
                    cost=cost,
                    continuation=continuation,
                    q_value=p * value - cost + continuation,
                    p_estimate_low=low,
                    p_estimate_high=high,
                )
            )
        return out

    # ------------------------------------------------------------------ public API

    def evaluate(
        self, observation: Observation, belief: Belief
    ) -> list[Candidate]:
        """Every admissible action, priced and ranked. The audit log takes this whole list,
        so a decision can be defended against the alternatives it beat."""
        economics = Economics(
            amount_inr=observation.amount_inr,
            rail=observation.rail,
            segment=observation.customer_history.segment_label,
        )
        state = DecisionState(
            belief=belief,
            attempts_used=observation.attempt_number - 1,
            contacts_used=observation.contacts_used,
            now=observation.observed_at,
        )
        candidates = self._candidates(observation, state, economics, self.config.max_depth)
        candidates.sort(key=lambda c: c.q_value, reverse=True)
        return candidates

    def best(self, observation: Observation, belief: Belief) -> Candidate:
        return self.evaluate(observation, belief)[0]
