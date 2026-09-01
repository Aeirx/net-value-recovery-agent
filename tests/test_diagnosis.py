"""The diagnosis layer: honest distributions, a fair floor, and no leaks.

The properties that matter here are not "is it accurate". They are:

1. Every arm returns a **valid distribution over all seven causes**, because the value
   engine marginalises over it and a malformed posterior would corrupt every Q-value.
2. The rules arm is a **fair floor** — good enough that beating it means something.
3. The evidence view **states facts, never conclusions**. A view that names the answer
   would make the LLM arm look good for the wrong reason.
4. Impossible causes get **exact zero**, and merely-unlikely ones never do.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from netvalue.agent.diagnose import evidence
from netvalue.agent.diagnose.llm import SYSTEM_PROMPT, LLMDiagnoser, response_schema
from netvalue.agent.diagnose.oracle import OracleDiagnoser, truth_map
from netvalue.agent.diagnose.rules import RulesDiagnoser
from netvalue.agent.diagnose.schema import (
    CARD_ONLY_CAUSES,
    CausePosterior,
    DiagnosedCause,
)
from netvalue.agent.observation import (
    CustomerHistory,
    Observation,
    ObservedErrorCode,
    ObservedRail,
    ObservedSegment,
)
from netvalue.eval.diagnosis import IDEAL_ACTION, evaluate, regret_matrix
from netvalue.eval.runner import to_observation
from netvalue.llm.cache import ResponseCache, request_key
from netvalue.llm.client import OfflineCacheMiss, StructuredClient
from netvalue.world.config import CONFIG_A, Cause
from netvalue.world.generator import generate_transactions

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def population() -> list:
    return generate_transactions(CONFIG_A.model_copy(update={"n_transactions": 250}))


@pytest.fixture(scope="module")
def observations(population: list) -> list[Observation]:
    return [
        to_observation(t, now=t.first_failure_at, attempt_number=1, contacts_used=0, prior=())
        for t in population
    ]


def _obs(**overrides: object) -> Observation:
    base: dict = dict(
        transaction_id="T-1", merchant_id="M", customer_id="C",
        rail=ObservedRail.CARD_MANDATE, amount_inr=149.0, plan_tenure_months=8,
        error_code=ObservedErrorCode.GW_21, error_message="instrument invalid",
        bank_id="BK_HDFC", card_last4="4242", card_network="VISA",
        card_exp_month=12, card_exp_year=2028, mandate_id="MND",
        mandate_created_at=datetime(2025, 7, 1), mandate_debits_this_cycle=1,
        attempt_number=1, prior_attempts=(),
        customer_history=CustomerHistory(
            successful_debits_12m=7, failed_debits_12m=0,
            last_success_at=datetime(2026, 2, 12), avg_days_late=0.6,
            prior_contact_responses=0, prior_contacts_sent=0,
            segment_label=ObservedSegment.ENGAGED,
        ),
        first_failure_at=datetime(2026, 3, 14, 9), expires_at=datetime(2026, 4, 4, 9),
        observed_at=datetime(2026, 3, 14, 9),
    )
    base.update(overrides)
    return Observation(**base)


# ------------------------------------------------------------------ the posterior


def test_posterior_must_cover_every_cause() -> None:
    with pytest.raises(ValueError, match="must cover every cause"):
        CausePosterior(probabilities={DiagnosedCause.RISK_BLOCK: 1.0})


def test_posterior_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match=r"sum to 1.0"):
        CausePosterior(probabilities={c: 0.5 for c in DiagnosedCause})


def test_from_weights_keeps_every_cause_off_hard_zero() -> None:
    """A hard zero is unrecoverable under a Bayesian update: no later evidence can move a
    cause off it. Reserve exact zeros for genuine impossibility."""
    p = CausePosterior.from_weights({DiagnosedCause.RISK_BLOCK: 1.0})
    assert all(v > 0.0 for v in p.probabilities.values())
    assert p.top is DiagnosedCause.RISK_BLOCK


def test_upi_zeroes_card_only_causes_exactly() -> None:
    """These are impossibilities, not improbabilities: a UPI mandate has no card."""
    p = CausePosterior.uniform().restricted_to_rail(ObservedRail.UPI_AUTOPAY)
    for cause in CARD_ONLY_CAUSES:
        assert p[cause] == 0.0
    assert p.probabilities[DiagnosedCause.RISK_BLOCK] > 0.0
    assert sum(p.probabilities.values()) == pytest.approx(1.0)


def test_entropy_reports_indecision() -> None:
    assert CausePosterior.uniform().entropy_bits > 2.7
    assert CausePosterior.point_mass(DiagnosedCause.RISK_BLOCK).entropy_bits == 0.0


# ------------------------------------------------------------------ the evidence view


def test_evidence_never_names_a_cause(observations: list[Observation]) -> None:
    """The view must present facts. If it named the answer, the LLM arm would be scoring
    a leak rather than reasoning."""
    banned = {c.value for c in DiagnosedCause}
    for obs in observations[:60]:
        text = evidence.build(obs).lower()
        for cause in banned:
            if obs.rail is ObservedRail.UPI_AUTOPAY and cause in {
                "card_expired", "route_degraded"
            }:
                continue  # named only in the rail-constraint note, as an exclusion
            assert cause not in text, f"evidence view names {cause}"


def test_evidence_carries_the_discriminating_signals() -> None:
    text = evidence.build(
        _obs(
            customer_history=CustomerHistory(
                successful_debits_12m=3, failed_debits_12m=4,
                last_success_at=datetime(2026, 1, 2), avg_days_late=7.3,
                prior_contact_responses=0, prior_contacts_sent=2,
                segment_label=ObservedSegment.LAPSED,
            )
        )
    )
    assert "7.3" in text, "average days late is the strongest cue and must appear"
    assert "NONE answered" in text
    assert "days to payday" in text
    assert "mandate expires in" in text


def test_evidence_flags_a_passed_expiry() -> None:
    text = evidence.build(_obs(card_exp_month=1, card_exp_year=2025))
    assert "ALREADY PASSED" in text


def test_evidence_states_the_upi_constraint() -> None:
    text = evidence.build(_obs(rail=ObservedRail.UPI_AUTOPAY, card_last4=None,
                               card_network=None, card_exp_month=None, card_exp_year=None))
    assert "impossible" in text.lower()


# ------------------------------------------------------------------ the rules arm


def test_rules_returns_a_valid_posterior_for_every_transaction(
    observations: list[Observation],
) -> None:
    diagnoser = RulesDiagnoser()
    for obs in observations:
        p = diagnoser.diagnose(obs)
        assert sum(p.probabilities.values()) == pytest.approx(1.0)
        assert p.rationale


def test_rules_beats_guessing_by_a_wide_margin(
    population: list, observations: list[Observation]
) -> None:
    """The floor has to be a fair opponent. If it were barely better than chance, beating
    it would prove nothing about the model arm."""
    diagnoser = RulesDiagnoser()
    posteriors = [diagnoser.diagnose(o) for o in observations]
    truths = [t.true_cause.value for t in population]
    report = evaluate(CONFIG_A, "rules", posteriors, truths)
    assert report.accuracy > 0.45, f"rules arm at {report.accuracy:.1%} is a strawman"


def test_rules_respects_the_rail(observations: list[Observation]) -> None:
    diagnoser = RulesDiagnoser()
    for obs in observations:
        if obs.rail is not ObservedRail.UPI_AUTOPAY:
            continue
        p = diagnoser.diagnose(obs)
        assert p[DiagnosedCause.CARD_EXPIRED] == 0.0
        assert p[DiagnosedCause.ROUTE_DEGRADED] == 0.0


def test_rules_reacts_to_a_passed_expiry() -> None:
    diagnoser = RulesDiagnoser()
    valid = diagnoser.diagnose(_obs())
    expired = diagnoser.diagnose(_obs(card_exp_month=3, card_exp_year=2024))
    assert expired[DiagnosedCause.CARD_EXPIRED] > valid[DiagnosedCause.CARD_EXPIRED]


def test_rules_reacts_to_a_late_payer() -> None:
    diagnoser = RulesDiagnoser()
    prompt_payer = diagnoser.diagnose(_obs(error_code=ObservedErrorCode.GW_05))
    late = diagnoser.diagnose(
        _obs(
            error_code=ObservedErrorCode.GW_05,
            customer_history=CustomerHistory(
                successful_debits_12m=7, failed_debits_12m=3,
                last_success_at=datetime(2026, 2, 12), avg_days_late=8.0,
                prior_contact_responses=0, prior_contacts_sent=0,
                segment_label=ObservedSegment.ENGAGED,
            ),
        )
    )
    assert (
        late[DiagnosedCause.INSUFFICIENT_FUNDS]
        > prompt_payer[DiagnosedCause.INSUFFICIENT_FUNDS]
    )


def test_rules_does_not_pretend_to_understand_an_unseen_code() -> None:
    """GW_99 has no documented meaning. Confidently guessing from it is exactly the
    failure config B is built to expose."""
    diagnoser = RulesDiagnoser()
    known = diagnoser.diagnose(_obs(error_code=ObservedErrorCode.GW_05))
    unknown = diagnoser.diagnose(_obs(error_code=ObservedErrorCode.GW_99))
    assert unknown.entropy_bits > known.entropy_bits


# ------------------------------------------------------------------ the oracle arm


def test_oracle_is_exact(population: list, observations: list[Observation]) -> None:
    diagnoser = OracleDiagnoser(truth_map([t.model_dump(mode="json") for t in population]))
    for obs, txn in zip(observations, population, strict=True):
        assert diagnoser.diagnose(obs).top.value == txn.true_cause.value


def test_oracle_refuses_to_guess_when_truth_is_missing() -> None:
    """A silent fallback would make a broken truth map look like a working oracle."""
    with pytest.raises(KeyError, match="no ground truth"):
        OracleDiagnoser({}).diagnose(_obs())


def test_degraded_oracle_places_the_stated_mass() -> None:
    diagnoser = OracleDiagnoser({"T-1": "risk_block"}, accuracy=0.8)
    p = diagnoser.diagnose(_obs())
    assert p[DiagnosedCause.RISK_BLOCK] == pytest.approx(0.8)


# ------------------------------------------------------------------ regret weighting


def test_regret_is_zero_on_the_diagonal() -> None:
    matrix = regret_matrix(CONFIG_A)
    for cause in Cause:
        assert matrix[(cause, cause)] == pytest.approx(0.0)


def test_regret_is_never_negative() -> None:
    """Acting on a wrong belief cannot beat acting on the truth."""
    assert all(v >= 0.0 for v in regret_matrix(CONFIG_A).values())


def test_confusions_differ_by_orders_of_magnitude() -> None:
    """The entire reason accuracy is the wrong metric here."""
    matrix = regret_matrix(CONFIG_A)
    off_diagonal = [v for (t, d), v in matrix.items() if t is not d and v > 0]
    assert max(off_diagonal) / min(off_diagonal) > 50


def test_abandoning_a_recoverable_card_costs_more_than_a_wasted_contact() -> None:
    """The GW_21 trade, in rupees: calling a live card dead loses the whole recovery,
    while calling a dead mandate a live card wastes one contact."""
    matrix = regret_matrix(CONFIG_A)
    lost_recovery = matrix[(Cause.CARD_EXPIRED, Cause.MANDATE_DEAD)]
    wasted_contact = matrix[(Cause.MANDATE_DEAD, Cause.CARD_EXPIRED)]
    assert lost_recovery > wasted_contact * 3


def test_every_cause_has_an_ideal_action() -> None:
    assert set(IDEAL_ACTION) == set(Cause)


# ------------------------------------------------------------------ the LLM plumbing


def test_cache_key_changes_with_every_input() -> None:
    base = {"model": "m", "prompt": "p", "params": {"a": 1}}
    k = request_key(**base)  # type: ignore[arg-type]
    assert k != request_key(model="m2", prompt="p", params={"a": 1})
    assert k != request_key(model="m", prompt="p2", params={"a": 1})
    assert k != request_key(model="m", prompt="p", params={"a": 2})


def test_cache_key_is_insensitive_to_dict_order() -> None:
    """Otherwise the cache would miss for no reason, and charge for it."""
    a = request_key(model="m", prompt="p", params={"x": 1, "y": 2})
    b = request_key(model="m", prompt="p", params={"y": 2, "x": 1})
    assert a == b


def test_cache_round_trips(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "c.sqlite")
    assert cache.get("k") is None
    cache.put("k", model="m", payload={"a": 1}, input_tokens=10, output_tokens=5)
    got = cache.get("k")
    assert got is not None
    assert got.payload == {"a": 1} and got.input_tokens == 10
    assert cache.stats()["entries"] == 1


def test_offline_client_refuses_to_spend_money(tmp_path: Path) -> None:
    """CI runs offline. A cache miss must fail loudly, not quietly bill a build machine."""
    client = StructuredClient(cache_path=tmp_path / "c.sqlite", offline=True)
    with pytest.raises(OfflineCacheMiss):
        client.complete_json(system="s", prompt="p", schema={"type": "object"})


def test_llm_arm_replays_from_cache_without_credentials(tmp_path: Path) -> None:
    """The whole point of committing the cache: the demo cannot fail on a dead network."""
    client = StructuredClient(cache_path=tmp_path / "c.sqlite", offline=True)
    obs = _obs()
    payload = {c.value: (0.7 if c is DiagnosedCause.CARD_EXPIRED else 0.05)
               for c in DiagnosedCause}
    payload["reasoning"] = "stored expiry has passed"  # type: ignore[assignment]
    key = request_key(
        model=client.model,
        prompt=evidence.build(obs),
        params={"system": SYSTEM_PROMPT, "schema": response_schema(),
                "effort": client.effort, "max_tokens": client.max_tokens},
    )
    client.cache.put(key, model=client.model, payload=payload)

    p = LLMDiagnoser(client).diagnose(obs)
    assert p.top is DiagnosedCause.CARD_EXPIRED
    assert p.source == "llm"
    assert client.usage.live_calls == 0


def test_llm_arm_survives_a_degenerate_response(tmp_path: Path) -> None:
    """All-zero probabilities are not a distribution. Falling back to 'we learned nothing'
    is right; inventing a confident answer would silently become the result."""
    client = StructuredClient(cache_path=tmp_path / "c.sqlite", offline=True)
    obs = _obs()
    key = request_key(
        model=client.model, prompt=evidence.build(obs),
        params={"system": SYSTEM_PROMPT, "schema": response_schema(),
                "effort": client.effort, "max_tokens": client.max_tokens},
    )
    client.cache.put(key, model=client.model,
                     payload={c.value: 0.0 for c in DiagnosedCause})
    p = LLMDiagnoser(client).diagnose(obs)
    assert sum(p.probabilities.values()) == pytest.approx(1.0)
    assert p.entropy_bits > 2.0, "a degenerate response must read as maximal uncertainty"


def test_response_schema_demands_all_seven_causes() -> None:
    schema = response_schema()
    for cause in DiagnosedCause:
        assert cause.value in schema["properties"]
        assert cause.value in schema["required"]
    assert schema["additionalProperties"] is False


def test_system_prompt_carries_no_calibrated_priors() -> None:
    """Handing the model the world's P(cause | code) table would be the same tautology as
    letting the agent import the simulator."""
    text = SYSTEM_PROMPT
    assert "0.5" not in text and "%" not in text.replace("90%", "").replace("60%", "")
    for code in ("GW_05", "GW_11", "GW_21", "GW_33", "GW_54", "GW_91"):
        assert code not in text, f"{code} prior leaked into the prompt"


def test_committed_report_matches_the_code() -> None:
    path = Path(__file__).resolve().parent.parent / "reports" / "diagnosis_a.json"
    if not path.exists():
        pytest.skip("diagnosis report not generated")
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["config_hash"] == CONFIG_A.config_hash()
