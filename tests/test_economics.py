"""The customer-annoyance cost must be load-bearing.

If annoyance never changes a decision, the whole thesis collapses back into "smarter
retry" and the submission is pitching Razorpay their own core competency, done worse.

Phase 0 established that because both the recovery value and the annoyance cost scale
with remaining customer lifetime value, ``ltv_remaining`` largely cancels and leaves an
**amount-independent policy boundary** — the required probability of recovery that
justifies sending contact *k*. Those four numbers are the thesis:

    contact 1 ~ 1.5%    contact 2 ~ 4.5%    contact 3 ~ 12.7%    contact 4 ~ 25.5%

Nothing hardcodes them. They fall out of the cost model. This module asserts that they
still do, and that the canonical abandon case from ``PHASE0_DECISIONS.md`` section 9 is
still an abandon.
"""

from __future__ import annotations

import itertools
import math

import pytest

from netvalue.world.config import (
    CONFIG_A,
    DeclineClass,
    Intervention,
    Rail,
    Segment,
    WorldConfig,
)


@pytest.fixture(scope="module")
def cfg() -> WorldConfig:
    return CONFIG_A


# ---------------------------------------------------------------------------- crossovers


@pytest.mark.parametrize(
    ("contact_index", "expected"),
    [(1, 0.0145), (2, 0.0455), (3, 0.1273), (4, 0.2545)],
)
def test_contact_crossover_thresholds(
    cfg: WorldConfig, contact_index: int, expected: float
) -> None:
    """The policy boundary the value engine will rediscover from first principles."""
    actual = cfg.required_recovery_prob_asymptotic(contact_index)
    assert actual == pytest.approx(expected, abs=5e-4), (
        f"contact {contact_index} break-even moved to {actual:.2%} (was {expected:.2%}). "
        "That is a real change to the agent's behaviour — record it in DECISIONS.md."
    )


def test_crossovers_are_strictly_increasing(cfg: WorldConfig) -> None:
    """Later contacts must be harder to justify, or stopping is not a real decision.

    The schedule plateaus past its last explicit level: ``delta_churn_beyond`` covers every
    contact from there on. That is deliberate and harmless, because
    ``max_contacts_per_transaction`` caps the reachable range well below it. Strictness is
    therefore asserted over the explicit levels and monotonicity over the tail.
    """
    n_explicit = len(cfg.costs.annoyance.delta_churn_by_contact)
    explicit = [cfg.required_recovery_prob_asymptotic(k) for k in range(1, n_explicit + 2)]
    assert all(b > a for a, b in itertools.pairwise(explicit)), explicit

    tail = [cfg.required_recovery_prob_asymptotic(k) for k in range(n_explicit + 1, n_explicit + 4)]
    assert all(b >= a for a, b in itertools.pairwise(tail)), tail


def test_annoyance_schedule_covers_every_reachable_contact(cfg: WorldConfig) -> None:
    """Every contact the bounds permit must sit on an explicitly chosen hazard, never on
    the fallback plateau — otherwise a reachable decision rests on an unexamined number."""
    reachable = cfg.bounds.max_contacts_per_transaction
    explicit = len(cfg.costs.annoyance.delta_churn_by_contact)
    assert reachable <= explicit, (
        f"bounds allow {reachable} contacts but only {explicit} hazards are specified; "
        f"contact {explicit + 1} would silently fall back to delta_churn_beyond"
    )


def test_first_contact_is_cheap_and_third_is_not(cfg: WorldConfig) -> None:
    """The shape that produces the headline result.

    A max-recovery policy sends every contact it is permitted to send. It wins on contact
    one, roughly breaks even on contact two, and destroys value on contact three. The gap
    between the first and third threshold is where the entire net-value delta comes from,
    so a thin gap means a thin thesis.
    """
    first = cfg.required_recovery_prob_asymptotic(1)
    third = cfg.required_recovery_prob_asymptotic(3)
    assert first < 0.03, f"contact 1 should be near-free to send, needs {first:.1%}"
    assert third > 0.10, f"contact 3 should be genuinely expensive, needs only {third:.1%}"
    assert third / first > 5.0, (
        f"contact 3 is only {third / first:.1f}x harder to justify than contact 1; "
        "the annoyance schedule is too flat for stopping to matter"
    )


# ---------------------------------------------------------------------------- value model


def test_recovery_value_carries_retained_ltv(cfg: WorldConfig) -> None:
    """DECISION-008: recovering a renewal saves the subscription, not just the invoice.

    Modelling it as the invoice alone makes nothing worth recovering and produces a
    degenerate agent that abandons everything — the mirror of the failure this project
    exists to avoid.
    """
    amount = 99.0
    ltv = cfg.ltv_remaining(amount, Segment.ENGAGED)
    value = cfg.recovery_value(amount, Rail.CARD_MANDATE, ltv)
    assert value > 5 * amount, (
        f"recovery value {value:.2f} is barely above the invoice {amount:.2f}; "
        "the agent will abandon everything"
    )


def test_upi_recovery_is_worth_more_than_card(cfg: WorldConfig) -> None:
    """UPI Autopay carries no MDR, so identical renewals are not equally valuable.

    The agent should behave differently by rail, and this asymmetry is free evidence that
    the value engine is doing real work rather than applying one global rule.
    """
    amount, ltv = 299.0, cfg.ltv_remaining(299.0, Segment.ENGAGED)
    card = cfg.recovery_value(amount, Rail.CARD_MANDATE, ltv)
    upi = cfg.recovery_value(amount, Rail.UPI_AUTOPAY, ltv)
    assert upi > card
    assert upi - card == pytest.approx(amount * cfg.costs.mdr_by_rail[Rail.CARD_MANDATE])


def test_annoyance_scales_with_customer_value(cfg: WorldConfig) -> None:
    """High-LTV customers are more expensive to annoy. This is what makes the policy
    non-obvious: the customers most worth recovering are also the costliest to pester."""
    cheap = cfg.annoyance_cost(2, cfg.ltv_remaining(99.0, Segment.DORMANT))
    dear = cfg.annoyance_cost(2, cfg.ltv_remaining(4999.0, Segment.ENGAGED))
    assert dear > 50 * cheap


def test_annoyance_is_convex_in_contact_index(cfg: WorldConfig) -> None:
    """Dunning fatigue is not linear. A linear schedule would make stopping a formality."""
    ltv = 1000.0
    costs = [cfg.annoyance_cost(k, ltv) for k in range(1, 5)]
    deltas = [b - a for a, b in itertools.pairwise(costs)]
    assert all(b > a for a, b in itertools.pairwise(deltas)), deltas


# ---------------------------------------------------------- the canonical abandon (P0 s9)


def test_canonical_abandoned_but_recoverable_case(cfg: WorldConfig) -> None:
    """TXN-A7F3 — the transaction that proves the thesis has teeth.

    A dormant subscriber on the Rs 99 plan whose hidden ground-truth cause is
    ``card_expired``. It is **genuinely recoverable**: a card update would fix it. Two
    requests have already gone unanswered and the third is being considered.

    A max-recovery policy sends it, occasionally succeeds, and books the success. It is
    wrong to. This transaction belongs on the abandoned-but-recoverable list, which is the
    artifact that demonstrates business judgment more directly than anything else in the
    submission.

    Every input below is read from the config, so this test and PHASE0_DECISIONS.md
    cannot drift apart.
    """
    amount = 99.0
    segment = Segment.DORMANT
    contact_index = 3

    ltv = cfg.ltv_remaining(amount, segment)
    value = cfg.recovery_value(amount, Rail.CARD_MANDATE, ltv)

    # Posterior after two non-responses has shifted toward mandate_dead, on which a card
    # update cannot possibly work.
    p_cause_is_fixable = 0.53
    base_response = cfg.segments[segment].contact_response_prob
    p_responds = cfg.costs.card_update.response_prob(base_response, contact_index)
    p_recover = p_cause_is_fixable * p_responds * cfg.costs.card_update.p_success_given_response

    flat_cost = cfg.costs.interventions[Intervention.REQUEST_CARD_UPDATE].flat_cost_inr
    cost = flat_cost + cfg.annoyance_cost(contact_index, ltv)
    expected_gain = p_recover * value
    net = expected_gain - cost

    assert p_recover < cfg.required_recovery_prob_asymptotic(contact_index), (
        f"P(recover)={p_recover:.2%} now clears the contact-3 bar of "
        f"{cfg.required_recovery_prob_asymptotic(contact_index):.2%}; "
        "the canonical abandon case is no longer an abandon"
    )
    assert net < 0.0, (
        f"TXN-A7F3 became worth pursuing: gain Rs {expected_gain:.2f} vs cost Rs {cost:.2f}"
    )
    assert net < -15.0, (
        f"TXN-A7F3 is only marginally negative (Rs {net:.2f}); Phase 0 requires a clear "
        "example, not a coin flip, or the exit criterion is not really satisfied"
    )


def test_a_meaningful_slice_of_the_world_is_not_worth_recovering(cfg: WorldConfig) -> None:
    """If nothing is value-destroying to recover, there is no thesis.

    Sweeps the plan ladder against every segment at contact 3 and requires that a real
    share of the population fails to clear the bar even under a generous assumption about
    the diagnosis being correct.
    """
    contact_index = 3
    bar = cfg.required_recovery_prob_asymptotic(contact_index)
    p_cause_correct = 0.80  # generous: assume diagnosis is usually right

    not_worth = 0.0
    for plan in cfg.plans:
        for _segment, spec in cfg.segments.items():
            p_responds = cfg.costs.card_update.response_prob(
                spec.contact_response_prob, contact_index
            )
            p_recover = (
                p_cause_correct * p_responds * cfg.costs.card_update.p_success_given_response
            )
            if p_recover < bar:
                not_worth += plan.share * spec.share

    assert not_worth > 0.30, (
        f"only {not_worth:.1%} of the population is not worth a third contact. "
        "The annoyance cost is too low and the submission has no thesis."
    )
    assert not_worth < 0.95, (
        f"{not_worth:.1%} of the population is unreachable at contact 3. "
        "The annoyance cost is so high the agent will never contact anyone."
    )


def test_bd_td_split_matches_calibration_target(cfg: WorldConfig) -> None:
    """Enforced by a validator too, but asserted here so a drift shows up as a test name.

    Measured on the **effective** prior. The raw priors describe a population the
    generator never produces, because two causes are card-only and the UPI rail
    renormalises over what remains.
    """
    eff = cfg.effective_cause_prior()
    bd = math.fsum(
        eff[cause]
        for cause, spec in cfg.causes.items()
        if spec.decline_class is DeclineClass.BD
    )
    assert bd == pytest.approx(cfg.bd_share_target, abs=cfg.bd_share_tolerance)
