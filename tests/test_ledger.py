"""Ledger conservation: recovered minus costs equals reported net value, exactly.

Net value is the headline number and the thesis is a subtraction, so an under-counted cost
would flatter the result in exactly the direction the project wants — and nothing in the
output would look wrong. These tests are the reason that cannot happen quietly.
"""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from netvalue.eval.ledger import Ledger, PostingKind, merge
from netvalue.eval.runner import EpisodeRunner
from netvalue.policies.max_recovery import (
    MaxRecoveryOraclePolicy,
    MaxRecoveryPolicy,
    build_truth,
)
from netvalue.policies.naive import NaiveRetryPolicy
from netvalue.policies.no_retry import NoRetryPolicy
from netvalue.world.banks import build_world_health
from netvalue.world.config import CONFIG_A
from netvalue.world.generator import generate_transactions

NOW = datetime(2026, 3, 10, 12, 0)


@pytest.fixture(scope="module")
def population() -> list:
    return generate_transactions(CONFIG_A.model_copy(update={"n_transactions": 120}))


# ---------------------------------------------------------------- the ledger itself


def test_net_value_is_the_sum_of_postings() -> None:
    ledger = Ledger()
    ledger.credit("T1", PostingKind.RECOVERED_VALUE, 500.0, NOW)
    ledger.debit("T1", PostingKind.ATTEMPT_COST, 1.2, NOW)
    ledger.debit("T1", PostingKind.ANNOYANCE_COST, 27.5, NOW)
    assert ledger.net_value() == pytest.approx(500.0 - 1.2 - 27.5)
    ledger.check_conservation()


def test_costs_are_stored_as_debits() -> None:
    ledger = Ledger()
    ledger.debit("T1", PostingKind.ATTEMPT_COST, 5.0, NOW)
    assert ledger.total(PostingKind.ATTEMPT_COST) == pytest.approx(-5.0)
    assert ledger.magnitude(PostingKind.ATTEMPT_COST) == pytest.approx(5.0)


def test_category_confusion_is_rejected() -> None:
    """A cost posted as a credit would inflate net value silently."""
    ledger = Ledger()
    with pytest.raises(ValueError):
        ledger.credit("T1", PostingKind.ATTEMPT_COST, 5.0, NOW)
    with pytest.raises(ValueError):
        ledger.debit("T1", PostingKind.RECOVERED_VALUE, 5.0, NOW)


def test_negative_magnitudes_are_rejected() -> None:
    ledger = Ledger()
    with pytest.raises(ValueError):
        ledger.debit("T1", PostingKind.ATTEMPT_COST, -5.0, NOW)


def test_conservation_detects_a_tampered_posting() -> None:
    """The invariant has to be able to fail, or asserting it proves nothing."""
    ledger = Ledger()
    ledger.credit("T1", PostingKind.RECOVERED_VALUE, 100.0, NOW)
    ledger.postings.append(
        ledger.postings[0].__class__("T1", PostingKind.ATTEMPT_COST, +7.0, NOW, "bad")
    )
    with pytest.raises(AssertionError, match="debit posted as positive"):
        ledger.check_conservation()


def test_merge_preserves_totals() -> None:
    a, b = Ledger(), Ledger()
    a.credit("T1", PostingKind.RECOVERED_VALUE, 100.0, NOW)
    b.debit("T2", PostingKind.ANNOYANCE_COST, 30.0, NOW)
    combined = merge([a, b])
    assert combined.net_value() == pytest.approx(70.0)
    combined.check_conservation()


# ---------------------------------------------------------------- end to end


@pytest.mark.parametrize("policy_name", ["no_retry", "naive", "max_recovery", "oracle"])
def test_every_episode_balances(population: list, policy_name: str) -> None:
    """Run a real policy over a real population and check every episode's arithmetic.

    ``run_one`` already calls ``check_conservation``; this re-derives net value from the
    reported components independently, so a bug in the ledger and a matching bug in the
    runner would still be caught.
    """
    cfg = CONFIG_A
    truth = build_truth(population)
    policy = {
        "no_retry": NoRetryPolicy(),
        "naive": NaiveRetryPolicy(),
        "max_recovery": MaxRecoveryPolicy(cfg, truth),
        "oracle": MaxRecoveryOraclePolicy(cfg, truth),
    }[policy_name]

    runner = EpisodeRunner(cfg, build_world_health(cfg), replication=0)
    results = runner.run(policy, population)

    for r in results:
        expected = r.recovered_value_inr - r.attempt_cost_inr - r.annoyance_cost_inr
        assert r.net_value_inr == pytest.approx(expected, abs=1e-6), r.transaction_id
        assert r.attempt_cost_inr >= 0.0
        assert r.annoyance_cost_inr >= 0.0
        if not r.recovered:
            assert r.recovered_value_inr == 0.0
            assert r.net_value_inr <= 0.0


def test_aggregate_net_value_equals_sum_of_episodes(population: list) -> None:
    cfg = CONFIG_A
    runner = EpisodeRunner(cfg, build_world_health(cfg), replication=0)
    results = runner.run(MaxRecoveryPolicy(cfg, build_truth(population)), population)

    total = math.fsum(r.net_value_inr for r in results)
    components = math.fsum(
        r.recovered_value_inr - r.attempt_cost_inr - r.annoyance_cost_inr for r in results
    )
    assert total == pytest.approx(components, abs=1e-6)


def test_no_retry_costs_nothing_and_earns_nothing(population: list) -> None:
    """The floor must be exactly zero, or every delta measured against it is offset."""
    runner = EpisodeRunner(CONFIG_A, build_world_health(CONFIG_A), replication=0)
    results = runner.run(NoRetryPolicy(), population)
    assert all(r.net_value_inr == 0.0 for r in results)
    assert all(r.attempts == 0 and r.contacts == 0 for r in results)


def test_a_contact_always_costs_annoyance(population: list) -> None:
    """If a contact could be free, the entire thesis would have nothing to trade against."""
    cfg = CONFIG_A
    runner = EpisodeRunner(cfg, build_world_health(cfg), replication=0)
    results = runner.run(MaxRecoveryPolicy(cfg, build_truth(population)), population)
    contacted = [r for r in results if r.contacts > 0]
    assert contacted, "no policy contact occurred, so annoyance is untested"
    assert all(r.annoyance_cost_inr > 0.0 for r in contacted)
