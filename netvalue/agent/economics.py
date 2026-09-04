"""The merchant's model of its own costs.

Declared agent-side rather than imported from ``world/config.py``, and the distinction is
not a technicality. A recovery system genuinely knows what a comms send costs it, what an
agent-hour costs, what its MDR is, and roughly what a subscriber is worth — those are its
own books. What it does *not* know is the physics of recovery: whether a retry on this
transaction will clear. That is what ``agent/estimator.py`` has to learn from outcomes.

So the boundary falls between **the merchant's own economics** (here, known) and **the
world's recovery behaviour** (hidden, estimated). Putting the cost model on the agent's
side of that line is the honest placement, and it is what lets the value engine price an
action at all.

These numbers mirror ``world/config.py`` because both describe the same business, and
``tests/test_value.py`` asserts they still agree — a silent divergence would have the agent
optimising a different business from the one it is scored in.
"""

from __future__ import annotations

from dataclasses import dataclass

from netvalue.agent.observation import ObservedRail, ObservedSegment
from netvalue.agent.policy import ActionKind

#: Merchant discount rate deducted from a recovered invoice. UPI Autopay carries none,
#: which is why a recovered UPI renewal is worth strictly more than an identical card one.
MDR_BY_RAIL: dict[ObservedRail, float] = {
    ObservedRail.CARD_MANDATE: 0.020,
    ObservedRail.UPI_AUTOPAY: 0.000,
}

#: Probability that a renewal never recovered becomes involuntary churn. This is what makes
#: a Rs 99 recovery worth far more than Rs 99: what is at stake is the subscription.
P_INVOLUNTARY_CHURN = 0.55

#: Expected remaining billing cycles by engagement segment. Sets customer lifetime value.
EXPECTED_REMAINING_CYCLES: dict[ObservedSegment, float] = {
    ObservedSegment.ENGAGED: 14.0,
    ObservedSegment.LAPSED: 7.0,
    ObservedSegment.DORMANT: 4.0,
}

#: Incremental churn hazard from the k-th customer contact, convex in k. Dunning fatigue is
#: not linear, and this convexity is what makes stopping a real decision rather than a
#: formality: the first contact is nearly free, the third rarely pays for itself.
DELTA_CHURN_BY_CONTACT: tuple[float, ...] = (0.008, 0.025, 0.070)
DELTA_CHURN_BEYOND = 0.140

#: Flat cost of taking each action, before any annoyance.
FLAT_COST_INR: dict[ActionKind, float] = {
    ActionKind.RETRY_NOW: 0.60,
    ActionKind.RETRY_AFTER: 0.60,
    ActionKind.SCHEDULE_RETRY_AT: 0.60,
    ActionKind.SWITCH_ROUTE_AND_RETRY: 0.90,
    ActionKind.REQUEST_CARD_UPDATE: 0.60,
    ActionKind.ESCALATE_TO_HUMAN: 85.00,
    ActionKind.ABANDON: 0.00,
}

#: Actions that spend a customer contact, and therefore incur annoyance.
CONTACT_ACTIONS: frozenset[ActionKind] = frozenset(
    {ActionKind.REQUEST_CARD_UPDATE, ActionKind.ESCALATE_TO_HUMAN}
)

#: Actions that present a debit rather than reaching the customer.
DEBIT_ACTIONS: frozenset[ActionKind] = frozenset(
    {
        ActionKind.RETRY_NOW,
        ActionKind.RETRY_AFTER,
        ActionKind.SCHEDULE_RETRY_AT,
        ActionKind.SWITCH_ROUTE_AND_RETRY,
    }
)


def delta_churn(contact_index: int) -> float:
    """Hazard added by the k-th contact. ``contact_index`` is 1-based."""
    if contact_index < 1:
        raise ValueError("contact_index is 1-based")
    if contact_index <= len(DELTA_CHURN_BY_CONTACT):
        return DELTA_CHURN_BY_CONTACT[contact_index - 1]
    return DELTA_CHURN_BEYOND


def ltv_remaining(amount_inr: float, segment: ObservedSegment) -> float:
    return amount_inr * EXPECTED_REMAINING_CYCLES[segment]


def recovery_value(amount_inr: float, rail: ObservedRail, ltv: float) -> float:
    """What a successful recovery is worth: the invoice net of MDR, plus the subscription
    it saves. Pricing it at the invoice alone would make almost nothing worth recovering
    and produce an agent that abandons everything."""
    return amount_inr * (1.0 - MDR_BY_RAIL[rail]) + P_INVOLUNTARY_CHURN * ltv


def annoyance_cost(contact_index: int, ltv: float) -> float:
    """Derived, never a chosen rupee figure: the churn hazard times what is at risk."""
    return delta_churn(contact_index) * ltv


def action_cost(action: ActionKind, *, contact_index: int, ltv: float) -> float:
    """Total cost of taking an action now, annoyance included."""
    cost = FLAT_COST_INR[action]
    if action in CONTACT_ACTIONS:
        cost += annoyance_cost(contact_index, ltv)
    return cost


def required_recovery_probability(contact_index: int) -> float:
    """The amount-independent bar a contact has to clear.

    Because both the value of a recovery and the cost of annoying a customer scale with
    remaining lifetime value, LTV largely cancels and leaves a clean threshold:
    roughly 1.5%, 4.6%, 12.7%, 25.5% for contacts one through four. Nothing hardcodes
    those; they fall out of the two constants above.
    """
    return delta_churn(contact_index) / P_INVOLUNTARY_CHURN


@dataclass(frozen=True, slots=True)
class Economics:
    """The cost model bound to one transaction, so the engine prices in one place."""

    amount_inr: float
    rail: ObservedRail
    segment: ObservedSegment

    @property
    def ltv(self) -> float:
        return ltv_remaining(self.amount_inr, self.segment)

    @property
    def value_of_recovery(self) -> float:
        return recovery_value(self.amount_inr, self.rail, self.ltv)

    def cost_of(self, action: ActionKind, *, contact_index: int) -> float:
        return action_cost(action, contact_index=contact_index, ltv=self.ltv)
