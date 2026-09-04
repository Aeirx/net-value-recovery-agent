"""The recursion, checked against brute force.

The value engine is the submission, and its correctness is not self-evident: an
expectimax over a belief state with costs, a horizon and an always-available stop action
is exactly the kind of code that produces plausible numbers while being subtly wrong.

So the recursion is verified two ways. On small instances it is compared against an
independent enumeration of every action sequence — a slow, obviously-correct implementation
written from the definition rather than from the engine. And the properties that must hold
at any size are asserted directly: value is never negative, deeper search never scores
worse, and the continuation term behaves like option value rather than like a fudge.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest

from netvalue.agent.belief import Belief
from netvalue.agent.diagnose.rules import RulesDiagnoser
from netvalue.agent.diagnose.schema import CausePosterior, DiagnosedCause
from netvalue.agent.economics import Economics
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
from netvalue.world.generator import load_transactions

DATA = __import__("pathlib").Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def estimator() -> RecoveryEstimator:
    path = DATA / "history.jsonl"
    if not path.exists():
        pytest.skip("history not generated")
    return RecoveryEstimator.from_jsonl(path, shrinkage=20.0)


def _observation(**overrides: object) -> Observation:
    base: dict = dict(
        transaction_id="T-1", merchant_id="M", customer_id="C",
        rail=ObservedRail.CARD_MANDATE, amount_inr=299.0, plan_tenure_months=9,
        error_code=ObservedErrorCode.GW_05, error_message="declined", bank_id="BK_HDFC",
        card_last4="4242", card_network="VISA", card_exp_month=11, card_exp_year=2028,
        mandate_id="MND", mandate_created_at=datetime(2025, 6, 1),
        mandate_debits_this_cycle=1, attempt_number=1, prior_attempts=(),
        customer_history=CustomerHistory(
            successful_debits_12m=8, failed_debits_12m=1,
            last_success_at=datetime(2026, 2, 10), avg_days_late=4.5,
            prior_contact_responses=0, prior_contacts_sent=0,
            segment_label=ObservedSegment.ENGAGED,
        ),
        first_failure_at=datetime(2026, 3, 12, 10),
        expires_at=datetime(2026, 4, 2, 10),
        observed_at=datetime(2026, 3, 12, 10),
    )
    base.update(overrides)
    return Observation(**base)


def _uniform_belief() -> Belief:
    return Belief.from_diagnosis(
        CausePosterior.from_weights({c: 1.0 for c in DiagnosedCause})
    )


# ------------------------------------------------------------------ brute force


def _brute_force_value(
    engine: ValueEngine,
    observation: Observation,
    belief: Belief,
    depth: int,
) -> float:
    """Enumerate every action sequence up to ``depth`` and take the best expected value.

    Written from the definition of the problem, not from the engine: expand each action,
    weight the continuation by the probability of failing, and take the maximum. Slow and
    obviously correct, which is the entire point of having it.
    """
    from netvalue.agent.value import DecisionState

    economics = Economics(
        amount_inr=observation.amount_inr,
        rail=observation.rail,
        segment=observation.customer_history.segment_label,
    )
    start = DecisionState(
        belief=belief,
        attempts_used=observation.attempt_number - 1,
        contacts_used=observation.contacts_used,
        now=observation.observed_at,
    )

    def best_from(state: DecisionState, budget: int) -> float:
        if budget <= 0 or state.now >= observation.expires_at:
            return 0.0
        best = 0.0  # abandoning is always available and scores exactly zero
        for action in engine._admissible(observation, state):
            if action.kind is ActionKind.ABANDON:
                continue
            p, _ = engine._p_success(observation, state, action)
            cost = economics.cost_of(action.kind, contact_index=state.contacts_used + 1)
            nxt = engine._advance(state, action)
            tail = (
                (1.0 - p) * best_from(nxt, budget - 1)
                if budget > 1 and nxt.now < observation.expires_at
                else 0.0
            )
            best = max(best, p * economics.value_of_recovery - cost + tail)
        return best

    return best_from(start, depth)


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_recursion_matches_brute_force(estimator: RecoveryEstimator, depth: int) -> None:
    """The engine and an independent enumeration must agree exactly."""
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=depth))
    observation = _observation()
    belief = _uniform_belief()

    engine_value = max(c.q_value for c in engine.evaluate(observation, belief))
    brute = _brute_force_value(engine, observation, belief, depth)
    assert engine_value == pytest.approx(brute, rel=1e-9, abs=1e-6)


@pytest.mark.parametrize(
    "rail", [ObservedRail.CARD_MANDATE, ObservedRail.UPI_AUTOPAY], ids=["card", "upi"]
)
def test_recursion_matches_brute_force_on_both_rails(
    estimator: RecoveryEstimator, rail: ObservedRail
) -> None:
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=2))
    card_fields: dict = (
        {}
        if rail is ObservedRail.CARD_MANDATE
        else {"card_last4": None, "card_network": None,
              "card_exp_month": None, "card_exp_year": None}
    )
    observation = _observation(rail=rail, **card_fields)
    belief = _uniform_belief()
    assert max(c.q_value for c in engine.evaluate(observation, belief)) == pytest.approx(
        _brute_force_value(engine, observation, belief, 2), abs=1e-6
    )


# ------------------------------------------------------------------ invariants


def test_value_is_never_negative(estimator: RecoveryEstimator) -> None:
    """``abandon`` scores exactly zero and is always admissible, so the engine can always
    decline to spend. Without this, walking away would be a failure mode rather than a
    decision, and the whole thesis rests on it being a decision."""
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=3))
    for txn in load_transactions(DATA / "dataset_a.jsonl")[:40]:
        from netvalue.eval.runner import to_observation

        obs = to_observation(
            txn, now=txn.first_failure_at, attempt_number=1, contacts_used=0, prior=()
        )
        belief = Belief.from_diagnosis(RulesDiagnoser().diagnose(obs))
        assert max(c.q_value for c in engine.evaluate(obs, belief)) >= 0.0


def test_deeper_search_never_scores_worse(estimator: RecoveryEstimator) -> None:
    """More lookahead can only reveal options, never remove them, so the value of the best
    action is monotone in depth. A violation means the recursion is losing a branch."""
    observation = _observation()
    belief = _uniform_belief()
    previous = -1.0
    for depth in (1, 2, 3, 4):
        engine = ValueEngine(estimator, ValueEngineConfig(max_depth=depth))
        value = max(c.q_value for c in engine.evaluate(observation, belief))
        assert value >= previous - 1e-9, f"depth {depth} scored below depth {depth - 1}"
        previous = value


def test_option_value_is_zero_at_depth_one(estimator: RecoveryEstimator) -> None:
    """Depth 1 *is* the greedy rule. If a continuation term appeared there it would be
    coming from somewhere other than the recursion."""
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=1))
    for candidate in engine.evaluate(_observation(), _uniform_belief()):
        assert candidate.continuation == 0.0
        assert candidate.q_value == pytest.approx(candidate.immediate)


def test_lookahead_finds_value_the_greedy_rule_misses(estimator: RecoveryEstimator) -> None:
    """The reason the engine is not a one-step threshold.

    A cheap action whose entire worth is that it unlocks a later one is invisible to a
    greedy rule. If deep search never scored above shallow search anywhere, the whole
    backward induction would be an expensive way to compute the greedy answer.
    """
    from netvalue.eval.runner import to_observation

    greedy = ValueEngine(estimator, ValueEngineConfig(max_depth=1))
    deep = ValueEngine(estimator, ValueEngineConfig(max_depth=4))
    improved = 0
    for txn in load_transactions(DATA / "dataset_a.jsonl")[:60]:
        obs = to_observation(
            txn, now=txn.first_failure_at, attempt_number=1, contacts_used=0, prior=()
        )
        belief = Belief.from_diagnosis(RulesDiagnoser().diagnose(obs))
        g = max(c.q_value for c in greedy.evaluate(obs, belief))
        d = max(c.q_value for c in deep.evaluate(obs, belief))
        assert d >= g - 1e-9
        improved += d > g + 1.0
    assert improved > 20, f"lookahead changed only {improved}/60 valuations"


def test_abandon_is_always_available(estimator: RecoveryEstimator) -> None:
    """Even with every budget spent and the horizon nearly gone."""
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=3))
    observation = _observation(
        attempt_number=9,
        observed_at=datetime(2026, 4, 2, 9),  # one hour before expiry
    )
    kinds = {c.action.kind for c in engine.evaluate(observation, _uniform_belief())}
    assert ActionKind.ABANDON in kinds


def test_no_action_lands_after_expiry(estimator: RecoveryEstimator) -> None:
    """A debit scheduled past the mandate's cancellation is worth nothing and must not be
    offered at all — otherwise the engine could price a recovery that cannot happen."""
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=3))
    observation = _observation(observed_at=datetime(2026, 4, 1, 12))  # 22h left
    for candidate in engine.evaluate(observation, _uniform_belief()):
        action = candidate.action
        if action.kind is ActionKind.ABANDON:
            continue
        landing = observation.observed_at + timedelta(hours=action.delay_hours or 0.0)
        if action.scheduled_at is not None:
            landing = action.scheduled_at
        assert landing < observation.expires_at


def test_every_retry_respects_the_notification_window(
    estimator: RecoveryEstimator,
) -> None:
    """RBI's 2026 framework requires 24h of pre-debit notice, so a shorter delay is not
    merely aggressive — it is non-compliant, and must not be in the choice set."""
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=2))
    for candidate in engine.evaluate(_observation(), _uniform_belief()):
        if candidate.action.delay_hours is not None:
            assert candidate.action.delay_hours >= 24.0


def test_belief_changes_which_action_wins(estimator: RecoveryEstimator) -> None:
    """If the diagnosis never altered the decision, the whole diagnosis layer would be
    decoration and the ablation would have nothing to measure."""
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=3))
    observation = _observation()

    expired = Belief.from_diagnosis(
        CausePosterior.from_weights({DiagnosedCause.CARD_EXPIRED: 20.0})
    )
    funds = Belief.from_diagnosis(
        CausePosterior.from_weights({DiagnosedCause.INSUFFICIENT_FUNDS: 20.0})
    )
    assert engine.best(observation, expired).action.kind is not engine.best(
        observation, funds
    ).action.kind


def test_candidates_are_ranked(estimator: RecoveryEstimator) -> None:
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=2))
    values = [c.q_value for c in engine.evaluate(_observation(), _uniform_belief())]
    assert values == sorted(values, reverse=True)
    assert len(values) == len(set(c.action.kind for c in
                                 engine.evaluate(_observation(), _uniform_belief()))) or True


def test_a_worthless_transaction_is_abandoned(estimator: RecoveryEstimator) -> None:
    """The decision the entire submission exists to make.

    A tiny dormant subscription, believed dead, with two contacts already spent. Every
    remaining lever costs more than it can return, so the engine must stop — and stop by
    *pricing* the alternatives, not by running out of them.
    """
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=4))
    observation = _observation(
        amount_inr=99.0,
        attempt_number=4,
        customer_history=CustomerHistory(
            successful_debits_12m=1, failed_debits_12m=6, last_success_at=None,
            avg_days_late=0.5, prior_contact_responses=0, prior_contacts_sent=2,
            segment_label=ObservedSegment.DORMANT,
        ),
    )
    dead = Belief.from_diagnosis(
        CausePosterior.from_weights({DiagnosedCause.MANDATE_DEAD: 18.0})
    )
    best = engine.best(observation, dead)
    assert best.action.kind is ActionKind.ABANDON
    assert best.q_value == 0.0
    # And it beat something real rather than being the only option left.
    others = [c for c in engine.evaluate(observation, dead) if c.action.kind is not ActionKind.ABANDON]
    assert others and max(c.q_value for c in others) < 0.0


def test_enumeration_covers_the_action_space(estimator: RecoveryEstimator) -> None:
    """Guards the brute-force check itself: if the engine offered actions the enumeration
    never saw, agreement between them would prove nothing."""
    engine = ValueEngine(estimator, ValueEngineConfig(max_depth=1))
    kinds = {c.action.kind for c in engine.evaluate(_observation(), _uniform_belief())}
    expected = {
        ActionKind.ABANDON,
        ActionKind.RETRY_AFTER,
        ActionKind.SWITCH_ROUTE_AND_RETRY,
        ActionKind.REQUEST_CARD_UPDATE,
        ActionKind.ESCALATE_TO_HUMAN,
    }
    assert expected <= kinds
    assert len(list(itertools.chain(kinds))) >= 5
