"""Belief over causes, updated by what the agent learns from acting.

The diagnoser gives a posterior once, from the evidence available at the first failure.
Then the agent *acts*, and every action returns information: a retry that fails is
evidence, and ignoring it means deciding attempt three on attempt one's beliefs.

**Attempts are experiments, not just costs.** That is the difference between a policy that
retries a dead mandate six times and one that infers it is dead after two.

The likelihood ``P(this action failed | cause)`` is hand-specified below. It has to be:
``agent/estimator.py`` is deliberately cause-agnostic — the historical log never recorded
*why* a payment failed, because the system that produced it never knew — so the update
cannot be sourced from there. See DECISION-056.

What justifies the numbers is that they are not really estimates. They are near-tautologies
of what each cause *is*: a retry cannot fix an expired card, so a failed retry is nearly
uninformative about that hypothesis and strongly informative against ``insufficient_funds``
timed at payday. Each one is stated with its reasoning, and
``tests/test_belief.py`` asserts the qualitative consequences rather than the values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from netvalue.agent.diagnose.schema import CausePosterior, DiagnosedCause
from netvalue.agent.policy import ActionKind

_C = DiagnosedCause

#: ``P(a debit attempt fails | cause)``. Every entry is a restatement of the cause.
#:
#: Read these as "how unsurprised am I that this failed, if the cause were X". A high value
#: means failure is expected under that hypothesis, so observing failure barely moves it; a
#: low value means failure is surprising, so observing it pushes that hypothesis down.
DEBIT_FAILURE_LIKELIHOOD: dict[DiagnosedCause, float] = {
    # Nothing can ever work. Failure is certain, so it is the hypothesis that survives
    # every failed attempt — which is exactly how a dead mandate should reveal itself.
    _C.MANDATE_DEAD: 0.99,
    # A retry cannot fix a dead instrument either. Failure is almost certain.
    _C.CARD_EXPIRED: 0.98,
    # Retrying into a risk block does essentially nothing.
    _C.RISK_BLOCK: 0.97,
    # A degraded route keeps failing on the same route. Switching is handled separately.
    _C.ROUTE_DEGRADED: 0.90,
    # An outage is total while it lasts, but the agent may have waited it out.
    _C.BANK_OUTAGE: 0.80,
    # A second presentation often completes. Failure is real evidence against it.
    _C.AFA_TIMEOUT: 0.72,
    # The most recoverable cause, so a failure is the most surprising and the most
    # informative. This is what makes repeated failures push the belief away from
    # "they will pay when they have money" and toward "the instrument is gone".
    _C.INSUFFICIENT_FUNDS: 0.70,
}

#: ``P(a route switch fails | cause)``. The point of switching is that it fixes exactly one
#: cause, so a failed switch is strong evidence that cause was not the problem.
ROUTE_SWITCH_FAILURE_LIKELIHOOD: dict[DiagnosedCause, float] = {
    **DEBIT_FAILURE_LIKELIHOOD,
    _C.ROUTE_DEGRADED: 0.28,
}

#: ``P(a card-update request produces no recovery | cause)``. Only an expired card can be
#: fixed this way, and even then only if the customer answers — so a silent contact is
#: weak evidence, not proof.
CARD_UPDATE_FAILURE_LIKELIHOOD: dict[DiagnosedCause, float] = {
    **{c: 0.995 for c in DiagnosedCause},
    _C.CARD_EXPIRED: 0.80,
}

#: ``P(a human escalation fails | cause)``. A human resolves most risk blocks and little
#: else, so a failed escalation argues against a risk block specifically.
ESCALATION_FAILURE_LIKELIHOOD: dict[DiagnosedCause, float] = {
    **{c: 0.95 for c in DiagnosedCause},
    _C.RISK_BLOCK: 0.28,
}

_LIKELIHOOD_BY_ACTION: dict[ActionKind, dict[DiagnosedCause, float]] = {
    ActionKind.RETRY_NOW: DEBIT_FAILURE_LIKELIHOOD,
    ActionKind.RETRY_AFTER: DEBIT_FAILURE_LIKELIHOOD,
    ActionKind.SCHEDULE_RETRY_AT: DEBIT_FAILURE_LIKELIHOOD,
    ActionKind.SWITCH_ROUTE_AND_RETRY: ROUTE_SWITCH_FAILURE_LIKELIHOOD,
    ActionKind.REQUEST_CARD_UPDATE: CARD_UPDATE_FAILURE_LIKELIHOOD,
    ActionKind.ESCALATE_TO_HUMAN: ESCALATION_FAILURE_LIKELIHOOD,
}

#: A retry timed for payday is a much sharper test of "they had no money" than one fired
#: mid-cycle: if the salary landed and it still failed, funds were probably not the issue.
_PAYDAY_SHARPENING = 0.55
_PAYDAY_DAYS = 2


@dataclass(frozen=True, slots=True)
class Belief:
    """The agent's current distribution over causes, and how it got here."""

    posterior: CausePosterior
    updates: int = 0

    @property
    def top(self) -> DiagnosedCause:
        return self.posterior.top

    @property
    def confidence(self) -> float:
        return self.posterior.confidence

    @property
    def entropy_bits(self) -> float:
        return self.posterior.entropy_bits

    def probability(self, cause: DiagnosedCause) -> float:
        return self.posterior[cause]

    def mass_on(self, causes: frozenset[DiagnosedCause]) -> float:
        return self.posterior.mass_on(causes)

    @classmethod
    def from_diagnosis(cls, posterior: CausePosterior) -> Belief:
        return cls(posterior=posterior, updates=0)

    # ------------------------------------------------------------------ the update

    def after_failure(
        self, action: ActionKind, *, days_to_payday: int | None = None
    ) -> Belief:
        """Bayes on the observation "I did this and it did not work".

        A cause the action could never have fixed is barely touched; one the action was
        well-suited to fix takes the hit. That asymmetry is the whole value of the update.
        """
        likelihood = _LIKELIHOOD_BY_ACTION.get(action)
        if likelihood is None:  # abandon carries no information
            return self

        weights: dict[DiagnosedCause, float] = {}
        for cause in DiagnosedCause:
            p_fail = likelihood[cause]
            if (
                cause is _C.INSUFFICIENT_FUNDS
                and days_to_payday is not None
                and days_to_payday <= _PAYDAY_DAYS
                and action
                in {
                    ActionKind.RETRY_NOW,
                    ActionKind.RETRY_AFTER,
                    ActionKind.SCHEDULE_RETRY_AT,
                }
            ):
                # Failing *on payday* is the sharpest available test of a funds problem.
                p_fail *= _PAYDAY_SHARPENING
            weights[cause] = self.posterior[cause] * p_fail

        if math.fsum(weights.values()) <= 0.0:
            return self

        return Belief(
            posterior=CausePosterior.from_weights(
                weights,
                rationale=(
                    f"{self.posterior.rationale} | updated on failed {action.value}"
                ).strip(" |"),
                source=self.posterior.source,
                floor=0.0,  # a cause already at zero is impossible, and stays so
            ),
            updates=self.updates + 1,
        )

    def describe(self) -> str:
        ranked = self.posterior.ranked()[:3]
        parts = [f"{c.value} {p:.0%}" for c, p in ranked]
        return f"{', '.join(parts)} (H={self.entropy_bits:.2f} bits, {self.updates} updates)"
