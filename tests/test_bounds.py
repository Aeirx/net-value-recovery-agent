"""The gate layer: property tests, because example tests cannot cover a constraint.

A bound is a claim about *every* input, so checking a handful of hand-picked ones proves
almost nothing. These generate inputs instead — including the awkward corners nobody thinks
to write down: zero hours left, every budget already spent, a belief concentrated on an
impossible cause.

Two families of property here:

**Safety.** No sequence of inputs produces a permitted action that breaks a cap, lands
inside the pre-debit notification window, or fires after the mandate is cancelled. And
``abandon`` survives every filter, because an agent with an empty action set would be
forced into a spend it had already decided against.

**Monotonicity.** Raising the cost of annoying customers must never make the agent contact
*more*; raising the value at stake must never make it try *less*. These are the directions
the economics claim to run in, and a violation would mean the engine is not responding to
its own cost model.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from netvalue.agent import bounds, economics
from netvalue.agent.belief import Belief
from netvalue.agent.diagnose.rules import RulesDiagnoser
from netvalue.agent.diagnose.schema import CausePosterior, DiagnosedCause
from netvalue.agent.estimator import RecoveryEstimator
from netvalue.agent.observation import (
    CustomerHistory,
    Observation,
    ObservedErrorCode,
    ObservedRail,
    ObservedSegment,
)
from netvalue.agent.policy import ActionKind
from netvalue.agent.value import ValueEngine, ValueEngineConfig
from netvalue.eval.runner import to_observation
from netvalue.world.generator import load_transactions

DATA = __import__("pathlib").Path(__file__).resolve().parent.parent / "data"
_SETTINGS = settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


@pytest.fixture(scope="module")
def estimator() -> RecoveryEstimator:
    path = DATA / "history.jsonl"
    if not path.exists():
        pytest.skip("history not generated")
    return RecoveryEstimator.from_jsonl(path, shrinkage=20.0)


def _observation(
    *,
    rail: ObservedRail = ObservedRail.CARD_MANDATE,
    amount: float = 299.0,
    attempt_number: int = 1,
    contacts_sent: int = 0,
    hours_left: float = 480.0,
    segment: ObservedSegment = ObservedSegment.ENGAGED,
) -> Observation:
    now = datetime(2026, 3, 12, 10)
    is_card = rail is ObservedRail.CARD_MANDATE
    return Observation(
        transaction_id="T-1", merchant_id="M", customer_id="C",
        rail=rail, amount_inr=amount, plan_tenure_months=9,
        error_code=ObservedErrorCode.GW_05, error_message="declined", bank_id="BK_HDFC",
        card_last4="4242" if is_card else None,
        card_network="VISA" if is_card else None,
        card_exp_month=11 if is_card else None,
        card_exp_year=2028 if is_card else None,
        mandate_id="MND", mandate_created_at=datetime(2025, 6, 1),
        mandate_debits_this_cycle=1, attempt_number=attempt_number, prior_attempts=(),
        customer_history=CustomerHistory(
            successful_debits_12m=8, failed_debits_12m=1,
            last_success_at=datetime(2026, 2, 10), avg_days_late=4.5,
            prior_contact_responses=0, prior_contacts_sent=contacts_sent,
            segment_label=segment,
        ),
        first_failure_at=now, expires_at=now + timedelta(hours=hours_left),
        observed_at=now,
    )


def _belief(cause: DiagnosedCause | None = None) -> Belief:
    weights = (
        {c: 1.0 for c in DiagnosedCause} if cause is None else {cause: 20.0}
    )
    return Belief.from_diagnosis(CausePosterior.from_weights(weights))


# ------------------------------------------------------------------ safety


@_SETTINGS
@given(
    attempt_number=st.integers(min_value=1, max_value=12),
    contacts_sent=st.integers(min_value=0, max_value=6),
    hours_left=st.floats(min_value=0.5, max_value=600.0),
    amount=st.floats(min_value=49.0, max_value=9999.0),
    rail=st.sampled_from(list(ObservedRail)),
    cause=st.sampled_from([None, *list(DiagnosedCause)]),
)
def test_no_permitted_action_ever_breaks_a_cap(
    estimator: RecoveryEstimator,
    attempt_number: int,
    contacts_sent: int,
    hours_left: float,
    amount: float,
    rail: ObservedRail,
    cause: DiagnosedCause | None,
) -> None:
    cfg = bounds.BoundsConfig()
    observation = _observation(
        rail=rail, amount=amount, attempt_number=attempt_number,
        contacts_sent=contacts_sent, hours_left=hours_left,
    )
    belief = _belief(cause)
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=2))
    result = bounds.apply(
        engine.evaluate(observation, belief), observation, belief, config=cfg
    )

    attempts_used = observation.attempt_number - 1
    debit_budget = min(cfg.max_attempts_per_transaction, cfg.max_debits_per_mandate_cycle)

    for candidate in result.allowed:
        kind = candidate.action.kind
        if kind is ActionKind.ABANDON:
            continue
        if kind in economics.DEBIT_ACTIONS:
            assert attempts_used < debit_budget
            landing = observation.observed_at + timedelta(
                hours=candidate.action.delay_hours or 0.0
            )
            if candidate.action.scheduled_at is not None:
                landing = candidate.action.scheduled_at
            assert landing < observation.expires_at
        if kind in economics.CONTACT_ACTIONS:
            assert observation.contacts_used < cfg.max_contacts_per_transaction
        if kind is ActionKind.REQUEST_CARD_UPDATE:
            assert observation.rail is ObservedRail.CARD_MANDATE


@_SETTINGS
@given(
    attempt_number=st.integers(min_value=1, max_value=12),
    contacts_sent=st.integers(min_value=0, max_value=6),
    hours_left=st.floats(min_value=0.1, max_value=600.0),
)
def test_abandon_survives_every_filter(
    estimator: RecoveryEstimator,
    attempt_number: int,
    contacts_sent: int,
    hours_left: float,
) -> None:
    """An empty action set would force a spend the agent had already declined."""
    observation = _observation(
        attempt_number=attempt_number, contacts_sent=contacts_sent, hours_left=hours_left
    )
    belief = _belief()
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=1))
    result = bounds.apply(engine.evaluate(observation, belief), observation, belief)
    assert any(c.action.kind is ActionKind.ABANDON for c in result.allowed)


@_SETTINGS
@given(last_debit_hours_ago=st.floats(min_value=0.0, max_value=48.0))
def test_no_debit_lands_inside_the_notification_window(
    estimator: RecoveryEstimator, last_debit_hours_ago: float
) -> None:
    """RBI's 2026 framework requires a fresh pre-debit notification 24h ahead of every
    attempt. A debit inside that window is not aggressive, it is non-compliant."""
    observation = _observation()
    belief = _belief()
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=1))
    last_debit = observation.observed_at - timedelta(hours=last_debit_hours_ago)
    result = bounds.apply(
        engine.evaluate(observation, belief), observation, belief, last_debit_at=last_debit
    )
    for candidate in result.allowed:
        if candidate.action.kind not in economics.DEBIT_ACTIONS:
            continue
        landing = observation.observed_at + timedelta(
            hours=candidate.action.delay_hours or 0.0
        )
        if candidate.action.scheduled_at is not None:
            landing = candidate.action.scheduled_at
        assert landing >= last_debit + timedelta(hours=24.0) - timedelta(seconds=1)


def test_a_believed_expired_card_stops_getting_plain_retries(
    estimator: RecoveryEstimator,
) -> None:
    """A retry cannot fix a dead instrument. Once the belief concentrates there, further
    plain retries are spending to learn something already known — so this is a hard block
    the engine may not outbid, not a preference."""
    observation = _observation(attempt_number=3)
    belief = _belief(DiagnosedCause.CARD_EXPIRED)
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=2))
    result = bounds.apply(engine.evaluate(observation, belief), observation, belief)
    kinds = {c.action.kind for c in result.allowed}
    assert ActionKind.RETRY_AFTER not in kinds
    assert "card_expired_retry_block" in result.fired
    # The lever that could actually work is still on the table.
    assert ActionKind.REQUEST_CARD_UPDATE in kinds


def test_a_stricter_gate_overrides_a_permissive_engine(
    estimator: RecoveryEstimator,
) -> None:
    """The gate layer is the authority, not the engine's own opinion of the rules.

    Both enforce the caps, which is defence in depth and means a well-configured engine
    rarely trips a gate. What must hold is that when they disagree the *gate* wins — the
    constraint layer cannot be something the value engine is able to out-argue.

    It also checks the gate is named. A policy blocked by a cap and one that chose to stop
    take the same action and are completely different decisions, and the audit log has to
    tell them apart.
    """
    observation = _observation(attempt_number=2, contacts_sent=0)
    belief = _belief()
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=1))
    candidates = engine.evaluate(observation, belief)
    assert any(c.action.kind in economics.DEBIT_ACTIONS for c in candidates)
    assert any(c.action.kind in economics.CONTACT_ACTIONS for c in candidates)

    strict = bounds.BoundsConfig(max_debits_per_mandate_cycle=1, max_contacts_per_transaction=0)
    result = bounds.apply(candidates, observation, belief, config=strict)

    kinds = {c.action.kind for c in result.allowed}
    assert kinds == {ActionKind.ABANDON}, "the gate failed to remove what it forbids"
    assert "mandate_cycle_cap" in result.fired
    assert "max_contacts" in result.fired
    assert result.gate_names


def test_the_layer_only_ever_shrinks(estimator: RecoveryEstimator) -> None:
    """A gate may remove an action. It may never add, substitute or reorder one — that
    would let the constraint layer quietly become the decision layer."""
    observation = _observation()
    belief = _belief()
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=2))
    candidates = engine.evaluate(observation, belief)
    result = bounds.apply(candidates, observation, belief)
    assert len(result.allowed) <= len(candidates)
    for allowed in result.allowed:
        assert any(allowed is original for original in candidates)


# ------------------------------------------------------------------ monotonicity


def _contacts_made(annoyance_scale: float, estimator: RecoveryEstimator) -> int:
    """Run the agent over a sample with the churn hazard scaled, and count contacts."""
    from netvalue.eval.runner import EpisodeRunner
    from netvalue.policies.net_value import build
    from netvalue.world.banks import build_world_health
    from netvalue.world.config import CONFIG_A

    original = economics.DELTA_CHURN_BY_CONTACT
    economics.DELTA_CHURN_BY_CONTACT = tuple(  # type: ignore[misc]
        min(v * annoyance_scale, 0.99) for v in original
    )
    try:
        txns = load_transactions(DATA / "dataset_a.jsonl")[:80]
        obs = [
            to_observation(t, now=t.first_failure_at, attempt_number=1,
                           contacts_used=0, prior=())
            for t in txns
        ]
        agent = build(RulesDiagnoser(), depth=3, reference_observations=obs)
        runner = EpisodeRunner(CONFIG_A, build_world_health(CONFIG_A), replication=0)
        return sum(r.contacts for r in runner.run(agent, txns))
    finally:
        economics.DELTA_CHURN_BY_CONTACT = original  # type: ignore[misc]


def test_costlier_annoyance_never_buys_more_contacts(
    estimator: RecoveryEstimator,
) -> None:
    """The direction the whole thesis claims to run in. If raising the price of annoying
    customers did not reduce contacts, the cost model would not be driving the policy and
    the Phase 8 sensitivity sweep would be measuring nothing."""
    cheap = _contacts_made(0.25, estimator)
    dear = _contacts_made(8.0, estimator)
    assert dear <= cheap, f"contacts rose from {cheap} to {dear} as annoyance got dearer"
    assert cheap > 0, "no contacts at any price means the lever is never used"


def test_the_contact_thresholds_are_monotone() -> None:
    """The four numbers the thesis rests on, asserted as a shape rather than as values."""
    bars = [economics.required_recovery_probability(k) for k in (1, 2, 3, 4)]
    assert bars == sorted(bars)
    assert bars[0] < 0.03 and bars[2] > 0.10


def test_a_bigger_subscription_is_never_less_worth_pursuing(
    estimator: RecoveryEstimator,
) -> None:
    """More at stake must not make the agent try less. The amount cancels out of the
    contact threshold, but it must still move the absolute value of acting."""
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=3))
    belief = _belief()
    small = engine.best(_observation(amount=99.0), belief)
    large = engine.best(_observation(amount=4999.0), belief)
    assert large.q_value >= small.q_value
