"""The harness itself: pairing, compliance, and the baselines behaving as designed.

Phase 4's job is to build a scoreboard that can be trusted before there is anything to
score. These tests protect the three properties that make it trustworthy.
"""

from __future__ import annotations

import pytest

from netvalue.agent.policy import Action, ActionKind
from netvalue.eval.bootstrap import cluster_bootstrap
from netvalue.eval.metrics import paired_deltas, summarise, win_rate
from netvalue.eval.runner import EpisodeRunner, to_observation
from netvalue.policies.max_recovery import (
    MaxRecoveryOraclePolicy,
    MaxRecoveryPolicy,
    build_truth,
)
from netvalue.policies.naive import NaiveRetryPolicy
from netvalue.policies.no_retry import NoRetryPolicy
from netvalue.world.banks import build_world_health
from netvalue.world.config import CONFIG_A, Cause
from netvalue.world.generator import generate_transactions


@pytest.fixture(scope="module")
def population() -> list:
    return generate_transactions(CONFIG_A.model_copy(update={"n_transactions": 150}))


def _run(policy_factory, population, replication: int = 0) -> list:
    health = build_world_health(CONFIG_A, replication)
    runner = EpisodeRunner(CONFIG_A, health, replication)
    return runner.run(policy_factory(), population)


# ------------------------------------------------------------------ the boundary


def test_the_observation_carries_no_ground_truth(population: list) -> None:
    """The runner's projection is the practical enforcement point for the boundary."""
    txn = population[0]
    obs = to_observation(
        txn, now=txn.first_failure_at, attempt_number=1, contacts_used=0, prior=()
    )
    dumped = obs.model_dump()
    for banned in ("true_cause", "cause", "recovery_probability", "acquirer_route"):
        assert banned not in dumped


# ------------------------------------------------------------------ pairing / CRN


def test_the_same_replication_gives_every_policy_the_same_world(population: list) -> None:
    """The property the whole comparison rests on.

    Two policies that behave differently must still meet identical luck at the same
    attempt index. If this fails, measured differences contain a large component of pure
    noise and no confidence interval can rescue them.
    """
    a1 = _run(NaiveRetryPolicy, population, replication=2)
    a2 = _run(NaiveRetryPolicy, population, replication=2)
    assert [r.net_value_inr for r in a1] == [r.net_value_inr for r in a2]


def test_different_replications_give_different_worlds(population: list) -> None:
    """Otherwise every replication is a copy and the confidence interval is a fiction."""
    r0 = _run(NaiveRetryPolicy, population, replication=0)
    r1 = _run(NaiveRetryPolicy, population, replication=1)
    assert [r.net_value_inr for r in r0] != [r.net_value_inr for r in r1]


def test_paired_deltas_align_on_transaction_and_replication(population: list) -> None:
    treatment = _run(NaiveRetryPolicy, population, replication=0)
    control = _run(NoRetryPolicy, population, replication=0)
    deltas = paired_deltas(treatment, control)
    assert len(deltas) == len(population)
    # no_retry is exactly zero everywhere, so the delta is the treatment's own net value.
    by_id = {r.transaction_id: r.net_value_inr for r in treatment}
    for (txn_id, _rep), delta in deltas.items():
        assert delta == pytest.approx(by_id[txn_id])


def test_bootstrap_interval_brackets_the_observed_total(population: list) -> None:
    treatment = _run(NaiveRetryPolicy, population, replication=0)
    control = _run(NoRetryPolicy, population, replication=0)
    interval = cluster_bootstrap(paired_deltas(treatment, control), n_resamples=500)
    assert interval.low <= interval.mean <= interval.high
    assert interval.n_clusters == len(population)


def test_bootstrap_on_a_constant_effect_resolves_its_sign() -> None:
    values = {(f"T{i}", 0): 10.0 for i in range(200)}
    interval = cluster_bootstrap(values, n_resamples=500)
    assert interval.excludes_zero
    assert interval.mean == pytest.approx(2000.0)


# ------------------------------------------------------------------ compliance


def test_a_retry_faster_than_the_floor_is_clamped_not_executed(population: list) -> None:
    """RBI's 2026 framework requires 24h of pre-debit notice before every attempt, so
    ``retry_now`` is inexpressible on these rails. The environment enforces that; the
    policy is not trusted to."""

    class ImpatientPolicy:
        name = "impatient"

        def decide(self, observation):  # type: ignore[no-untyped-def]
            if observation.attempt_number > 2:
                return Action(kind=ActionKind.ABANDON)
            return Action(kind=ActionKind.RETRY_NOW, rationale="illegal on this rail")

        def reset(self) -> None:
            return None

    results = _run(ImpatientPolicy, population[:40])
    assert sum(r.gate_fires for r in results) > 0, "the floor was never enforced"

    floor = CONFIG_A.bounds.min_inter_attempt_hours
    for r in results:
        debits = [row for row in r.rows if row.delay_hours is not None]
        for row in debits:
            assert row.delay_hours is not None
            assert row.delay_hours >= floor - 1e-9, "a debit landed inside the notice window"


def test_no_policy_exceeds_the_hard_caps(population: list) -> None:
    bounds = CONFIG_A.bounds
    truth = build_truth(population)
    for factory in (
        NoRetryPolicy,
        NaiveRetryPolicy,
        lambda: MaxRecoveryPolicy(CONFIG_A, truth),
        lambda: MaxRecoveryOraclePolicy(CONFIG_A, truth),
    ):
        for r in _run(factory, population):
            assert r.attempts <= bounds.max_attempts_per_transaction
            assert r.attempts <= bounds.max_debits_per_mandate_cycle
            assert r.contacts <= bounds.max_contacts_per_transaction


def test_nothing_acts_after_the_mandate_expires(population: list) -> None:
    truth = build_truth(population)
    for r in _run(lambda: MaxRecoveryPolicy(CONFIG_A, truth), population):
        txn = truth[r.transaction_id]
        for row in r.rows:
            assert row.at <= txn.expires_at


# ------------------------------------------------------------------ baselines behave


def test_the_ceiling_actually_spends_on_contacts(population: list) -> None:
    """A max-recovery ceiling that never contacts anyone incurs no annoyance cost, and
    then the thesis has nothing to trade against.

    This was a real bug: the cycle cap terminated episodes before any contact could be
    made, so the ceiling looked cheap and cost-free. The test exists so it cannot recur.
    """
    truth = build_truth(population)
    metrics = summarise(
        "max_recovery", _run(lambda: MaxRecoveryPolicy(CONFIG_A, truth), population)
    )
    assert metrics.total_contacts > 0
    assert metrics.annoyance_cost_inr > 0.0


def test_the_ceiling_recovers_more_than_naive(population: list) -> None:
    """It ignores cost and uses every lever, so it must win on gross by construction."""
    truth = build_truth(population)
    naive = summarise("naive", _run(NaiveRetryPolicy, population))
    ceiling = summarise(
        "max_recovery", _run(lambda: MaxRecoveryPolicy(CONFIG_A, truth), population)
    )
    assert ceiling.gross_recovered_inr > naive.gross_recovered_inr
    assert ceiling.recovery_rate > naive.recovery_rate


def test_the_oracle_never_wastes_effort_on_dead_mandates(population: list) -> None:
    truth = build_truth(population)
    for r in _run(lambda: MaxRecoveryOraclePolicy(CONFIG_A, truth), population):
        if truth[r.transaction_id].true_cause is Cause.MANDATE_DEAD:
            assert r.attempts == 0 and r.contacts == 0


def test_no_retry_is_the_floor(population: list) -> None:
    metrics = summarise("no_retry", _run(NoRetryPolicy, population))
    assert metrics.net_value_inr == 0.0
    assert metrics.recovery_rate == 0.0
    assert metrics.abandoned_but_recoverable > 0


def test_win_rate_ignores_ties() -> None:
    deltas = {("A", 0): 5.0, ("B", 0): -5.0, ("C", 0): 0.0, ("D", 0): 0.0}
    assert win_rate(deltas) == pytest.approx(0.5)
